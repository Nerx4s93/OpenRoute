"""Integration tests for the gateway (main.py) through TestClient — NO real network.

The upstream is replaced by a ScriptedTransport (the router scripts responses),
proxy fetching is disabled, and retry sleeps are recorded in ``gw.sleeps``
instead of being awaited. Chat upstream always answers SSE (the opencode gate
forces streaming); callers may still ask for a non-stream answer, which the
gateway converts to a standard OpenAI non-stream ``chat.completion`` object.
"""

import gzip
import json
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

import app.main as main
import app.proxies as prox

from gw import TrackingStream

AUTH = {"Authorization": "Bearer test-key"}
SSE_HEADERS = {"content-type": "text/event-stream"}
FREE_MODEL = "deepseek-v4.1-flash-free"

# A two-chunk SSE completion with usage in the final chunk.
SSE_CHUNKS = [
    b'data: {"id":"cmpl-1","object":"chat.completion.chunk","model":"deepseek-v4.1-flash-free",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n\n',
    b'data: {"id":"cmpl-1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":6,"completion_tokens":2,"total_tokens":8}}\n\n',
    b"data: [DONE]\n\n",
]
SSE_BODY = b"".join(SSE_CHUNKS)


def body(model=FREE_MODEL, **over):
    d = {"model": model, "messages": [{"role": "user", "content": "ping"}]}
    d.update(over)
    return d


def sse_response(chunks=None):
    return httpx.Response(200, headers=SSE_HEADERS, content=(b"".join(chunks) if chunks else SSE_BODY))


def ok_response():
    return httpx.Response(200, json={"id": "cmpl-1", "object": "chat.completion", "choices": []})


def upstream_json(transport, i=0):
    return json.loads(transport.requests[i].read())


# --- health ---


def test_health_open(make_gateway):
    gw = make_gateway()
    r = gw.client.get("/health")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok", "provider": "OpenRoute",
        "proxies_total": 2, "proxies_enabled": 2, "proxies_manually_disabled": 0,
        "proxies_cooling": 0, "proxies_rpm_capped": 0, "last_proxies_fetch": None,
        "egress_groups_total": 2, "egress_groups_ready": 2,
        "egress_groups_quota_parked": 0, "egress_groups_banned": 0,
        "sessions_bound": 0, "sessions_unbound": 0,
    }


def test_health_counters_honest(make_gateway):
    gw = make_gateway(settings_over={"openroute_rpm_limit": 1})
    a = gw.proxy("http://proxy-a.invalid:8080")
    b = gw.proxy("http://proxy-b.invalid:8080")
    a.cooldown_until = time.monotonic() + 30.0  # cooling
    gw.manager.set_manually_disabled(b.url, True)  # manual off
    # also hit the RPM cap on a's peer: record on the still-enabled one
    gw.manager.record_request(a)

    j = gw.client.get("/health").json()
    assert j["proxies_total"] == 2
    assert j["proxies_enabled"] == 1        # b manual-disabled
    assert j["proxies_manually_disabled"] == 1
    assert j["proxies_cooling"] == 1        # a
    assert j["proxies_rpm_capped"] == 1     # a


# --- auth ---


def test_wrong_client_key_401_openai_style(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())

    r = gw.client.post("/v1/chat/completions", json=body(),
                       headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["message"] == "Invalid OpenRoute API key" and err["code"] == 401

    r2 = gw.client.post("/v1/chat/completions", json=body())
    assert r2.status_code == 401
    assert tr.requests == []  # upstream never touched


# --- non-stream conversion + gate injection ---


def test_nonstream_chat_is_converted_from_sse(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())

    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "hi"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"] == {"prompt_tokens": 6, "completion_tokens": 2, "total_tokens": 8}
    assert r.headers["content-type"].startswith("application/json")

    a = gw.proxy("http://proxy-a.invalid:8080")
    assert a.requests == 1 and a.errors == 0  # success recorded

    # upstream request was gated correctly
    sent = tr.requests[0]
    assert sent.headers["authorization"] == "Bearer public"
    assert sent.headers["x-opencode-client"] == "cli"
    assert sent.headers["user-agent"].startswith("opencode/")
    sid = sent.headers["x-opencode-session"]
    for h in ("x-session-affinity", "x-session-id", "x-opencode-session-id"):
        assert sent.headers[h] == sid
    up = upstream_json(tr)
    assert up["model"] == FREE_MODEL
    assert up["stream"] is True
    assert up["stream_options"] == {"include_usage": True}
    assert up["messages"][0]["role"] == "system"
    assert main.opencode.KEY1 in up["messages"][0]["content"]
    assert up["messages"][-1] == {"role": "user", "content": "ping"}


def test_gate_injection_preserves_caller_system(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    payload = body(messages=[
        {"role": "system", "content": "Answer in French."},
        {"role": "user", "content": "salut"},
    ])
    r = gw.client.post("/v1/chat/completions", json=payload, headers=AUTH)
    assert r.status_code == 200
    up = upstream_json(tr)
    sysmsg = up["messages"][0]
    assert sysmsg["role"] == "system"
    assert sysmsg["content"].startswith(main.opencode.KEY1)
    assert sysmsg["content"].rstrip().endswith("Answer in French.")
    # exactly one system message, caller messages preserved in order
    assert sum(1 for m in up["messages"] if m["role"] == "system") == 1
    assert up["messages"][1] == {"role": "user", "content": "salut"}


# --- upstream session identity ---


def test_gate_forwards_caller_session_id(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    sid = "ses_0123456789ab" + "A" * 14

    r = gw.client.post("/v1/chat/completions", json=body(),
                       headers={**AUTH, "x-opencode-session": sid})
    assert r.status_code == 200
    sent = tr.requests[0]
    for name in main.opencode.SESSION_HEADER_NAMES:
        assert sent.headers[name] == sid


def test_gate_reuses_session_across_turns(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())

    first = body(messages=[{"role": "user", "content": "hello"}])
    second = body(messages=[
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "again"},
    ])
    assert gw.client.post("/v1/chat/completions", json=first,
                          headers=AUTH).status_code == 200
    assert gw.client.post("/v1/chat/completions", json=second,
                          headers=AUTH).status_code == 200
    assert tr.requests[0].headers["x-opencode-session"] == \
        tr.requests[1].headers["x-opencode-session"]


def test_gate_new_conversation_gets_new_session(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    gw.client.post(
        "/v1/chat/completions",
        json=body(messages=[{"role": "user", "content": "a"}]), headers=AUTH,
    )
    gw.client.post(
        "/v1/chat/completions",
        json=body(messages=[{"role": "user", "content": "b"}]), headers=AUTH,
    )
    assert tr.requests[0].headers["x-opencode-session"] != \
        tr.requests[1].headers["x-opencode-session"]


def test_gate_retry_reuses_session(make_gateway):
    gw = make_gateway()
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": "internal"})
        return sse_response()

    tr = gw.script_upstream(router)
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers=AUTH).status_code == 200
    assert len(tr.requests) == 2
    assert tr.requests[0].headers["x-opencode-session"] == \
        tr.requests[1].headers["x-opencode-session"]


def test_gate_replaces_invalid_caller_session_id(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", json=body(),
                       headers={**AUTH, "x-opencode-session": "abc123"})
    assert r.status_code == 200
    sent = tr.requests[0].headers["x-opencode-session"]
    assert sent != "abc123"
    assert main.opencode.is_session_id(sent)


def test_session_sticky_keeps_same_proxy_across_turns(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    sid = "ses_0123456789ab" + "C" * 14
    h = {**AUTH, "x-opencode-session": sid}

    first = body(messages=[{"role": "user", "content": "hello"}])
    second = body(messages=[
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "again"},
    ])
    assert gw.client.post("/v1/chat/completions", json=first, headers=h).status_code == 200
    assert gw.client.post("/v1/chat/completions", json=second, headers=h).status_code == 200

    a = gw.proxy("http://proxy-a.invalid:8080")
    b = gw.proxy("http://proxy-b.invalid:8080")
    assert a.requests == 2 and b.requests == 0  # one stable exit IP
    state = gw.affinity._sessions[f"sid:{sid}"]
    assert state.proxy == a.url
    assert len(state.events) == 2
    assert sum(e.tokens for e in state.events) == 16  # 8 real tokens twice


def test_admin_status_exposes_host_only_affinity_groups(make_gateway):
    gw = make_gateway()
    gw.script_upstream(lambda req: sse_response())
    sid = "ses_0123456789ab" + "E" * 14
    r = gw.client.post("/v1/chat/completions", json=body(),
                       headers={**AUTH, "x-opencode-session": sid})
    assert r.status_code == 200

    aff = gw.client.get("/admin/status", headers=AUTH).json()["affinity"]
    assert aff["enabled"] is True
    assert aff["sessions_total"] == 1
    assert aff["sessions_bound"] == 1 and aff["sessions_unbound"] == 0
    assert len(aff["egress_groups"]) == 2
    bound = next(g for g in aff["egress_groups"] if g["sessions"] == 1)
    assert bound["proxy_count"] == 1
    assert bound["requests_1h"] == 1 and bound["tokens_1h"] == 8
    # Host-only snapshot: masked host:port entries, no scheme or credentials.
    assert bound["proxies"] == [f"{bound['host']}:8080"]
    assert "://" not in json.dumps(aff)


def test_different_sessions_are_balanced_across_egress_groups(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    for i in range(4):
        sid = f"ses_{i:012x}" + "D" * 14
        r = gw.client.post("/v1/chat/completions", json=body(),
                           headers={**AUTH, "x-opencode-session": sid})
        assert r.status_code == 200
    a = gw.proxy("http://proxy-a.invalid:8080")
    b = gw.proxy("http://proxy-b.invalid:8080")
    assert a.requests >= 1 and b.requests >= 1
    assert len(tr.requests) == 4
    # 4 bound sessions, evenly distributed (2/2).
    assert a.requests == 2 and b.requests == 2


def test_default_model_when_caller_omits_it(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions",
                       json={"messages": [{"role": "user", "content": "x"}]}, headers=AUTH)
    assert r.status_code == 200
    assert upstream_json(tr)["model"] == gw.settings.opencode_model


# --- model allow-list ---


def test_non_free_model_403(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", json=body(model="gpt-4o"), headers=AUTH)
    assert r.status_code == 403
    assert "not allowed" in r.json()["error"]["message"]
    assert tr.requests == []


def test_key_model_allow_list_403(make_gateway):
    gw = make_gateway(keys=[{"key": "sk-m", "key_id": "m1", "models": [FREE_MODEL]}])
    tr = gw.script_upstream(lambda req: sse_response())
    mh = {"Authorization": "Bearer sk-m"}

    r = gw.client.post("/v1/chat/completions", json=body(model="beam-free"), headers=mh)
    assert r.status_code == 403
    assert "not allowed" in r.json()["error"]["message"]
    assert tr.requests == []

    r2 = gw.client.post("/v1/chat/completions", json=body(), headers=mh)
    assert r2.status_code == 200


# --- 429 classification: generic pass-through, daily quota parking ---


def test_generic_429_passthrough_no_cooldown_no_rotation(make_gateway):
    """Provider/key 429 is NOT an exit-IP property: pass it through untouched."""
    gw = make_gateway()
    payload = {"error": {"type": "RateLimitError", "message": "key per-minute limit"}}

    def router(req):
        return httpx.Response(429, headers={"Retry-After": "7"}, json=payload)

    tr = gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)

    assert r.status_code == 429
    assert r.json() == payload  # upstream body passed through verbatim
    assert r.headers["retry-after"] == "7"
    assert len(tr.requests) == 1  # no rotation to the next proxy
    a = gw.proxy("http://proxy-a.invalid:8080")
    assert a.requests == 1 and a.errors == 0
    assert a.cooldown_until == 0.0
    assert all(g.quota_until == 0.0 and g.banned_until == 0.0
               for g in gw.manager.egress_groups())
    assert gw.sleeps == []


def test_429_garbage_retry_after_and_unknown_body_pass_through(make_gateway):
    gw = make_gateway()

    def router(req):
        return httpx.Response(429, headers={"Retry-After": "not-a-number"}, json={})

    tr = gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 429
    assert r.json() == {}
    assert len(tr.requests) == 1
    assert gw.proxy("http://proxy-a.invalid:8080").cooldown_until == 0.0


def test_429_http_date_is_passed_through(make_gateway):
    gw = make_gateway()
    target = datetime.now(timezone.utc) + timedelta(seconds=15)
    header = format_datetime(target, usegmt=True)

    def router(req):
        return httpx.Response(429, headers={"Retry-After": header},
                              json={"error": {"type": "RateLimitError"}})

    tr = gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 429
    assert r.headers["retry-after"] == header
    assert len(tr.requests) == 1
    assert gw.proxy("http://proxy-a.invalid:8080").cooldown_until == 0.0


def test_cooldown_proxy_is_skipped_then_reused_after_expiry(make_gateway, monkeypatch):
    """Manual/breaker cooldown still gates the pool (one-shot non-chat calls)."""
    gw = make_gateway()
    clock = {"now": 10_000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])

    def router(req):
        return ok_response()

    tr = gw.script_upstream(router)
    a = gw.proxy("http://proxy-a.invalid:8080")
    b = gw.proxy("http://proxy-b.invalid:8080")
    a.cooldown_until = clock["now"] + 7.0

    for _ in range(2):
        assert gw.client.get("/v1/probe", headers=AUTH).status_code == 200
    assert a.requests == 0 and b.requests == 2

    clock["now"] += 7.1
    assert gw.client.get("/v1/probe", headers=AUTH).status_code == 200
    assert a.requests == 1


def test_free_usage_limit_parks_group_and_evacuates_current_session(make_gateway):
    gw = make_gateway()
    sid = "ses_0123456789ab" + "A" * 14
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "30"}, json={
                "type": "error",
                "error": {"type": "FreeUsageLimitError", "message": "daily limit"},
            })
        return sse_response()

    tr = gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(),
                       headers={**AUTH, "x-opencode-session": sid})
    assert r.status_code == 200
    assert len(tr.requests) == 2  # quota on A -> immediate retry on B

    groups = gw.manager.egress_groups()
    a, b = groups[0], groups[1]
    expected = max(main.opencode.seconds_to_utc_midnight(), 30.0) \
        + gw.settings.quota_reset_margin_seconds
    assert a.quota_until - time.time() == pytest.approx(expected, abs=5.0)
    assert a.banned_until == 0.0
    a_proxy = a.proxies[0]
    assert a_proxy.errors == 0 and a_proxy.cooldown_until == 0.0  # no mark_failure

    state = gw.affinity._sessions[f"sid:{sid}"]
    assert gw.manager.group_of(state.proxy) is b
    assert b.proxies[0].requests == 1
    gw.flush_affinity()
    row = next(r for r in gw.affinitydb.load_all() if r[0] == f"sid:{sid}")
    assert row[1] == state.proxy


def test_generic_429_does_not_park_or_evacuate(make_gateway):
    gw = make_gateway()
    sid = "ses_0123456789ab" + "B" * 14

    def router(req):
        return httpx.Response(429, json={
            "error": {"type": "RateLimitError", "message": "provider rate"}})

    tr = gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(),
                       headers={**AUTH, "x-opencode-session": sid})
    assert r.status_code == 429
    assert len(tr.requests) == 1
    state = gw.affinity._sessions[f"sid:{sid}"]
    assert state.proxy is not None  # binding untouched
    assert all(g.quota_until == 0.0 for g in gw.manager.egress_groups())


def test_all_groups_parked_429_with_retry_after(make_gateway):
    gw = make_gateway()
    until = time.time() + 77.0
    for group in gw.manager.egress_groups():
        gw.manager.mark_quota_exhausted(group, until)

    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 429
    assert float(r.headers["retry-after"]) == pytest.approx(77, abs=2)
    assert r.json()["error"]["type"] == "rate_limit_error"
    assert tr.requests == []


def test_no_ready_after_all_attempts_is_502(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: httpx.Response(500, json={"error": "x"}))
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 502
    assert len(tr.requests) == 2


# --- 5xx / 401 failover, 502, 504 ---


def test_first_proxy_500_second_200(make_gateway):
    gw = make_gateway()
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": "internal"})
        return sse_response()

    gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 200
    a = gw.proxy("http://proxy-a.invalid:8080")
    b = gw.proxy("http://proxy-b.invalid:8080")
    assert a.errors == 1 and "upstream HTTP 500" in (a.last_error or "")
    assert b.errors == 0 and b.requests == 1
    assert gw.sleeps == [0.5]


def test_upstream_401_failure_and_retries(make_gateway):
    gw = make_gateway()
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"error": "unauthorized"})
        return sse_response()

    gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 200
    a = gw.proxy("http://proxy-a.invalid:8080")
    assert a.errors == 1 and "HTTP 401" in (a.last_error or "")
    assert a.disabled is False  # proxies are never permanently dead
    assert gw.proxy("http://proxy-b.invalid:8080").requests == 1


def test_upstream_free_tier_403_passthrough(make_gateway):
    """A body-level FreeTierError 403 is passed through, not retried as a proxy failure."""
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: httpx.Response(403, json={
        "type": "error",
        "error": {"type": "FreeTierError",
                  "message": "OpenCode's free tier can only be used from within OpenCode."},
    }))
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["code"] == 403 and err["type"] == "upstream_error"
    assert "can only be used from within OpenCode" in err["message"]
    assert len(tr.requests) == 1  # every proxy answers the same: no failover
    a = gw.proxy("http://proxy-a.invalid:8080")
    assert a.errors == 0 and a.cooldown_until == 0.0  # pool stays healthy
    assert gw.sleeps == []


def test_upstream_403_non_free_tier_still_fails_over(make_gateway):
    """A 403 without the FreeTierError envelope keeps the old failover path."""
    gw = make_gateway()
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, content=b"Forbidden by egress IP")
        return sse_response()

    gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 200
    a = gw.proxy("http://proxy-a.invalid:8080")
    assert a.errors == 1 and "HTTP 403" in (a.last_error or "")
    assert gw.proxy("http://proxy-b.invalid:8080").requests == 1


def test_abusive_traffic_403_bans_group_and_evacuates(make_gateway):
    gw = make_gateway()
    sid = "ses_0123456789ab" + "E" * 14
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(403, content=b"Access denied: Abusive Traffic")
        return sse_response()

    tr = gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(),
                       headers={**AUTH, "x-opencode-session": sid})
    assert r.status_code == 200
    assert len(tr.requests) == 2  # banned A -> retry on B

    a, b = gw.manager.egress_groups()
    assert a.banned_until - time.time() == pytest.approx(
        gw.settings.abusive_ban_seconds, abs=5.0)
    assert a.quota_until == 0.0
    assert a.proxies[0].errors == 0  # not a proxy failure
    state = gw.affinity._sessions[f"sid:{sid}"]
    assert gw.manager.group_of(state.proxy) is b


def test_free_tier_403_is_not_an_abusive_ban(make_gateway):
    gw = make_gateway()

    def router(req):
        return httpx.Response(403, json={
            "type": "error",
            "error": {"type": "FreeTierError",
                      "message": "OpenCode's free tier can only be used from within OpenCode."},
        })

    gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 403
    assert all(g.banned_until == 0.0 for g in gw.manager.egress_groups())



def test_all_proxies_fail_502(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: httpx.Response(500, json={"error": "internal"}))
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["message"] == "All upstream attempts failed" and err["code"] == 502
    assert len(tr.requests) == 2  # each proxy exactly once
    assert gw.sleeps == [0.5, 1.0]
    assert gw.proxy("http://proxy-a.invalid:8080").requests == 1
    assert gw.proxy("http://proxy-b.invalid:8080").requests == 1


def test_retry_budget_exhausted_504(make_gateway):
    gw = make_gateway(settings_over={"retry_budget_seconds": 0.0})
    tr = gw.script_upstream(lambda req: httpx.Response(500, json={"error": "internal"}))
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 504
    assert r.json()["error"]["message"] == "Upstream retry budget exhausted"
    assert len(tr.requests) == 1


# --- saturated / empty pool ---


def test_pool_saturated_first_pick_429_with_retry_after(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    now = time.monotonic()
    for url in ("http://proxy-a.invalid:8080", "http://proxy-b.invalid:8080"):
        gw.proxy(url).cooldown_until = now + 42.0

    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "42"
    err = r.json()["error"]
    assert err["code"] == 429 and err["type"] == "rate_limit_error"
    assert tr.requests == []


def test_no_proxies_503(make_gateway):
    gw = make_gateway(proxies=())
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 503
    assert "No enabled proxies" in r.json()["error"]["message"]
    assert tr.requests == []


def test_no_proxies_503_message_is_generic(make_gateway):
    """The empty-pool 503 must not leak the internal fetch URL/credentials."""
    gw = make_gateway(proxies=())
    gw.manager.last_fetch_status = (
        "error: 500 for url 'http://svc:supersecret@proxy6-service:8000/proxies'"
    )
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == 503
    assert err["message"] == "No enabled proxies"
    assert "http://" not in err["message"]
    assert "supersecret" not in r.text and "proxy6-service" not in r.text
    assert tr.requests == []


def test_health_and_admin_status_redact_fetch_credentials(make_gateway):
    """last_proxies_fetch (public /health + admin /admin/status) strips userinfo."""
    gw = make_gateway(proxies=())
    gw.manager.last_fetch_status = (
        "error: 500 for url 'http://svc:supersecret@proxy6-service:8000/proxies'"
    )
    h = gw.client.get("/health").json()
    assert "supersecret" not in json.dumps(h)
    assert "http://***@proxy6-service:8000/proxies" in h["last_proxies_fetch"]

    a = gw.client.get("/admin/status", headers=AUTH).json()
    assert "supersecret" not in json.dumps(a)
    assert "http://***@proxy6-service:8000/proxies" in a["last_proxies_fetch"]


# --- local RPM cap ---


def test_local_rpm_cap_rotates_then_429(make_gateway):
    gw = make_gateway(settings_over={"openroute_rpm_limit": 1})
    tr = gw.script_upstream(lambda req: sse_response())

    r1 = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    r2 = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r1.status_code == 200 and r2.status_code == 200
    a = gw.proxy("http://proxy-a.invalid:8080")
    b = gw.proxy("http://proxy-b.invalid:8080")
    assert len(a._rpm) == 1 and len(b._rpm) == 1

    r3 = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r3.status_code == 429
    assert int(r3.headers["retry-after"]) >= 1
    assert len(tr.requests) == 2  # upstream not touched for the third


# --- body limits ---


def test_body_too_large_413(make_gateway):
    gw = make_gateway(settings_over={"max_body_bytes": 1024})
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", content=b"x" * 4096,
                       headers={**AUTH, "content-type": "application/json"})
    assert r.status_code == 413
    assert "exceeds" in r.json()["error"]["message"]
    assert tr.requests == []


def test_body_too_large_chunked_incremental_413(make_gateway):
    gw = make_gateway(settings_over={"max_body_bytes": 16})
    tr = gw.script_upstream(lambda req: sse_response())

    def huge():
        yield b"x" * 64

    r = gw.client.post("/v1/chat/completions", content=huge(),
                       headers={**AUTH, "content-type": "application/json"})
    assert r.status_code == 413
    assert tr.requests == []


# --- admin auth / limits ---


def test_admin_403_for_non_admin_key(make_gateway):
    gw = make_gateway(keys=[
        {"key": "sk-user", "key_id": "u1", "admin": False},
        {"key": "sk-adm", "key_id": "a1", "admin": True},
    ])
    gw.script_upstream(lambda req: sse_response())
    r = gw.client.get("/admin/status", headers={"Authorization": "Bearer sk-user"})
    assert r.status_code == 403
    assert r.json()["error"]["message"] == "Admin access required"
    r2 = gw.client.get("/admin/status", headers={"Authorization": "Bearer sk-adm"})
    assert r2.status_code == 200
    assert r2.json()["keys"] == ["a1", "u1"]


def test_admin_calls_do_not_consume_rpm(make_gateway):
    gw = make_gateway(keys=[{"key": "sk-r", "key_id": "r1", "admin": True, "rpm": 2}])
    gw.script_upstream(lambda req: sse_response())
    h = {"Authorization": "Bearer sk-r"}
    for _ in range(5):
        assert gw.client.get("/admin/status", headers=h).status_code == 200
    assert gw.client.post("/v1/chat/completions", json=body(), headers=h).status_code == 200
    assert gw.client.post("/v1/chat/completions", json=body(), headers=h).status_code == 200
    assert gw.client.post("/v1/chat/completions", json=body(), headers=h).status_code == 429


# --- client TPM ---


def test_tpm_pre_gate_429_with_retry_after(make_gateway):
    gw = make_gateway(keys=[{"key": "sk-t", "key_id": "t1", "tpm": 100}])
    tr = gw.script_upstream(lambda req: sse_response())
    k = gw.keystore.authenticate("Bearer sk-t")
    gw.keystore.count_tokens(k, 100)

    r = gw.client.post("/v1/chat/completions", json=body(), headers={"Authorization": "Bearer sk-t"})
    assert r.status_code == 429
    assert "retry-after" in r.headers and int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["type"] == "rate_limit_error"
    assert tr.requests == []


def test_tpm_usage_counted_from_converted_usage(make_gateway):
    gw = make_gateway(keys=[{"key": "sk-t", "key_id": "t1", "tpm": 100}])
    tr = gw.script_upstream(lambda req: sse_response())
    h = {"Authorization": "Bearer sk-t"}

    r1 = gw.client.post("/v1/chat/completions", json=body(), headers=h)
    assert r1.status_code == 200  # usage total 8 < 100

    # force the window over the limit deterministically, then the next request gates
    k = gw.keystore.authenticate("Bearer sk-t")
    gw.keystore.count_tokens(k, 95)
    r2 = gw.client.post("/v1/chat/completions", json=body(), headers=h)
    assert r2.status_code == 429
    assert len(tr.requests) == 1


# --- streaming passthrough ---


def test_chat_stream_passthrough(make_gateway):
    gw = make_gateway()
    stream = TrackingStream(SSE_CHUNKS)

    def router(req):
        return httpx.Response(200, headers=SSE_HEADERS, stream=stream)

    gw.script_upstream(router)
    a = gw.proxy("http://proxy-a.invalid:8080")
    a.consecutive_errors = 2  # bad past — must be cleared by a completed stream

    with gw.client.stream("POST", "/v1/chat/completions",
                          json=body(stream=True), headers=AUTH) as r:
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/event-stream"
        received = b"".join(r.iter_raw())

    assert received == SSE_BODY
    assert a.consecutive_errors == 0
    assert stream.closed is True


def test_mid_stream_failure_marks_failure_and_closes(make_gateway):
    gw = make_gateway()
    stream = TrackingStream([b'data: {"x": 1}\n\n', b'data: {"y": 2}\n\n'], fail_after=1)

    def router(req):
        return httpx.Response(200, headers=SSE_HEADERS, stream=stream)

    gw.script_upstream(router)
    a = gw.proxy("http://proxy-a.invalid:8080")

    with pytest.raises(httpx.HTTPError):
        with gw.client.stream("POST", "/v1/chat/completions",
                              json=body(stream=True), headers=AUTH) as r:
            assert r.status_code == 200
            b"".join(r.iter_raw())

    assert a.consecutive_errors == 1
    assert "stream error" in (a.last_error or "")
    assert stream.closed is True and stream.failed is True


def test_stream_gzip_decompressed_and_header_dropped(make_gateway):
    gw = make_gateway()
    gz = gzip.compress(SSE_BODY)

    def router(req):
        return httpx.Response(200, headers={**SSE_HEADERS, "content-encoding": "gzip"},
                              stream=httpx.ByteStream(gz))

    gw.script_upstream(router)
    with gw.client.stream("POST", "/v1/chat/completions",
                          json=body(stream=True), headers=AUTH) as r:
        assert r.status_code == 200
        assert "content-encoding" not in r.headers
        received = b"".join(r.iter_raw())
    assert received == SSE_BODY  # client got DECOMPRESSED bytes


# --- header hygiene / params ---


def test_upstream_headers_hygiene(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post(
        "/v1/chat/completions", json=body(),
        headers={**AUTH, "x-forwarded-for": "9.9.9.9", "cookie": "session=1",
                 "x-api-key": "leak-me", "x-real-ip": "8.8.8.8"},
    )
    assert r.status_code == 200
    sent = tr.requests[0]
    assert sent.headers["authorization"] == "Bearer public"  # client key never forwarded
    for h in ("x-forwarded-for", "cookie", "x-api-key", "x-real-ip"):
        assert h not in sent.headers


def test_repeated_query_params_forwarded(make_gateway):
    gw = make_gateway()
    seen = {}

    def router(req):
        seen["url"] = str(req.url)
        return sse_response()

    gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions?a=1&a=2&b=3", json=body(), headers=AUTH)
    assert r.status_code == 200
    assert seen["url"].endswith("?a=1&a=2&b=3")


# --- /v1/models ---


def test_models_endpoint_uses_catalogue(make_gateway, monkeypatch):
    gw = make_gateway()

    async def fake():
        return {"object": "list", "data": [{"id": "beam-free", "object": "model"}]}

    monkeypatch.setattr(main, "_fetch_models_catalogue", fake)
    r = gw.client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["data"][0]["id"] == "beam-free"
    assert gw.client.get("/v1/models").status_code == 401  # auth still required


def test_models_endpoint_fallback_on_error(make_gateway, monkeypatch):
    gw = make_gateway()

    async def boom():
        raise RuntimeError("no network")

    monkeypatch.setattr(main, "_fetch_models_catalogue", boom)
    r = gw.client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["data"]}
    assert FREE_MODEL in ids
    assert r.json()["object"] == "list"


# --- logging / aggregate ---


def _wait_log_entry(gw, pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for row in gw.client.get("/admin/logs?limit=20", headers=AUTH).json():
            if pred(row):
                return row
        time.sleep(0.05)
    raise AssertionError("log entry never appeared in reqlog")


def test_admin_logs_and_usage_persisted(make_gateway):
    gw = make_gateway()
    gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 200

    row = _wait_log_entry(gw, lambda x: x["path"] == "chat/completions")
    assert row["key_id"] == "default" and row["status"] == 200
    assert row["method"] == "POST" and row["model"] == FREE_MODEL
    assert row["account"] == "proxy-a.invalid:8080"  # masked proxy
    assert row["prompt_tokens"] == 6 and row["completion_tokens"] == 2
    usage = gw.client.get("/admin/status", headers=AUTH).json()["usage"]
    assert usage["total"]["requests"] >= 1


def test_stream_usage_logged(make_gateway):
    gw = make_gateway()
    stream = TrackingStream(SSE_CHUNKS)

    def router(req):
        return httpx.Response(200, headers=SSE_HEADERS, stream=stream)

    gw.script_upstream(router)
    with gw.client.stream("POST", "/v1/chat/completions",
                          json=body(stream=True), headers=AUTH) as r:
        assert r.status_code == 200
        b"".join(r.iter_raw())
    row = _wait_log_entry(gw, lambda x: x["stream"] is True and x["status"] == 200)
    assert row["prompt_tokens"] == 6 and row["completion_tokens"] == 2


def test_stream_affinity_records_real_usage_on_completion(make_gateway):
    gw = make_gateway()
    sid = "ses_0123456789ab" + "F" * 14
    stream = TrackingStream(SSE_CHUNKS)

    def router(req):
        return httpx.Response(200, headers=SSE_HEADERS, stream=stream)

    gw.script_upstream(router)
    with gw.client.stream("POST", "/v1/chat/completions",
                          json=body(stream=True),
                          headers={**AUTH, "x-opencode-session": sid}) as r:
        assert r.status_code == 200
        b"".join(r.iter_raw())

    state = gw.affinity._sessions[f"sid:{sid}"]
    assert len(state.events) == 1  # one client request -> one event
    assert state.events[0].tokens == 8  # real total_tokens, not bytes/4
    assert gw.proxy(state.proxy).consecutive_errors == 0


def test_stream_failure_keeps_captured_real_usage_but_no_success(make_gateway):
    gw = make_gateway()
    sid = "ses_0123456789ab" + "G" * 14
    usage_chunk = SSE_CHUNKS[1]
    stream = TrackingStream([usage_chunk, b"data: {\"x\":1}\n\n"], fail_after=1)

    def router(req):
        return httpx.Response(200, headers=SSE_HEADERS, stream=stream)

    gw.script_upstream(router)
    with pytest.raises(httpx.HTTPError):
        with gw.client.stream("POST", "/v1/chat/completions",
                              json=body(stream=True),
                              headers={**AUTH, "x-opencode-session": sid}) as r:
            assert r.status_code == 200
            b"".join(r.iter_raw())

    state = gw.affinity._sessions[f"sid:{sid}"]
    assert state.events[0].tokens == 8  # usage arrived before the disconnect
    proxy = gw.proxy(state.proxy)
    assert proxy.consecutive_errors == 1  # no mark_success on a broken stream


def test_stream_failure_without_usage_records_zero_tokens(make_gateway):
    gw = make_gateway()
    sid = "ses_0123456789ab" + "H" * 14
    stream = TrackingStream([SSE_CHUNKS[0], b"data: {\"x\":1}\n\n"], fail_after=1)

    def router(req):
        return httpx.Response(200, headers=SSE_HEADERS, stream=stream)

    gw.script_upstream(router)
    with pytest.raises(httpx.HTTPError):
        with gw.client.stream("POST", "/v1/chat/completions",
                              json=body(stream=True),
                              headers={**AUTH, "x-opencode-session": sid}) as r:
            assert r.status_code == 200
            b"".join(r.iter_raw())

    state = gw.affinity._sessions[f"sid:{sid}"]
    assert state.events[0].tokens == 0
    assert gw.proxy(state.proxy).consecutive_errors == 1


# --- internal API ---


def test_edge_hides_internal(make_gateway):
    gw = make_gateway()
    assert gw.client.get("/internal/proxies-data").status_code == 404
    assert gw.internal.get("/internal/proxies-data").status_code == 403  # token missing


def test_internal_requires_token(make_gateway):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}
    r = gw.internal.get("/internal/proxies-data", headers=h)
    assert r.status_code == 200
    data = r.json()
    assert set(data) >= {"status", "usage", "usage_by_proxy", "recent"}
    assert data["status"]["proxies_total"] == 2

    bad = gw.internal.get("/internal/proxies-data", headers={"X-Internal-Token": "nope"})
    assert bad.status_code == 403
    assert bad.json()["error"]["message"] == "Invalid internal token"


def test_internal_disabled_when_token_empty(make_gateway):
    gw = make_gateway(settings_over={"internal_api_token": ""})
    h = {"X-Internal-Token": "test-internal-token"}
    r = gw.internal.get("/internal/proxies-data", headers=h)
    assert r.status_code == 404


def test_internal_proxies_add_endpoint_removed(make_gateway):
    """Manual add is gone: pool membership comes only from the PROXIES_URL refresh."""
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}
    r = gw.internal.post("/internal/proxies/add", json={"proxies": "http://n1:1"}, headers=h)
    assert r.status_code == 404


def test_internal_proxies_set_delete(make_gateway):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}

    r = gw.internal.post("/internal/proxies/set",
                         json={"proxy": "http://proxy-a.invalid:8080", "enabled": False},
                         headers=h)
    assert r.status_code == 200
    row = next(p for p in gw.internal.get("/internal/proxies-data", headers=h).json()["status"]["proxies"]
               if p["real_proxy"] == "http://proxy-a.invalid:8080")
    assert row["enabled"] is False and row["manual"] is True

    r2 = gw.internal.post("/internal/proxies/delete",
                          json={"proxy": "http://proxy-b.invalid:8080"}, headers=h)
    assert r2.status_code == 200
    with pytest.raises(StopIteration):
        next(p for p in gw.manager.proxies if p.url == "http://proxy-b.invalid:8080")

    r3 = gw.internal.post("/internal/proxies/delete", json={"proxy": "http://nope:1"}, headers=h)
    assert r3.status_code == 404


def test_internal_disable_and_delete_invalidate_affinity(make_gateway):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}
    sid = "ses_0123456789ab" + "F" * 14
    gw.script_upstream(lambda req: sse_response())
    auth = {**AUTH, "x-opencode-session": sid}

    assert gw.client.post("/v1/chat/completions", json=body(), headers=auth).status_code == 200
    state = gw.affinity._sessions[f"sid:{sid}"]
    first = state.proxy
    assert first
    other = next(p.url for p in gw.manager.proxies if p.url != first)

    r = gw.internal.post("/internal/proxies/set",
                         json={"proxy": first, "enabled": False}, headers=h)
    assert r.status_code == 200
    assert state.proxy is None  # stale sticky binding invalidated at once
    gw.flush_affinity()
    row = next(r for r in gw.affinitydb.load_all() if r[0] == f"sid:{sid}")
    assert row[1] is None  # persisted as proxy = NULL, row/created_at preserved

    # The next turn leaves the disabled exit and binds to the remaining one.
    assert gw.client.post("/v1/chat/completions", json=body(), headers=auth).status_code == 200
    assert state.proxy == other

    r2 = gw.internal.post("/internal/proxies/delete", json={"proxy": other}, headers=h)
    assert r2.status_code == 200
    assert state.proxy is None
    gw.flush_affinity()
    row = next(r for r in gw.affinitydb.load_all() if r[0] == f"sid:{sid}")
    assert row[1] is None


def test_internal_keys_add_set_delete_and_persist(make_gateway):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}

    r = gw.internal.post("/internal/keys/add",
                         json={"key": "sk-new", "key_id": "n1", "admin": True, "rpm": 5},
                         headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True
    # the new key works immediately
    gw.script_upstream(lambda req: sse_response())
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers={"Authorization": "Bearer sk-new"}).status_code == 200

    data = gw.internal.get("/internal/keys-data", headers=h).json()
    row = next(k for k in data["keys"] if k["key_id"] == "n1")
    assert row["admin"] is True and row["rpm"] == 5

    r2 = gw.internal.post("/internal/keys/set",
                          json={"key_hash": row["key_hash"], "enabled": False}, headers=h)
    assert r2.status_code == 200
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers={"Authorization": "Bearer sk-new"}).status_code == 401

    r3 = gw.internal.post("/internal/keys/delete", json={"key_hash": row["key_hash"]}, headers=h)
    assert r3.status_code == 200


def test_internal_bad_json_body_400(make_gateway):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}
    r = gw.internal.post("/internal/keys/add", content=b"not-json",
                         headers={**h, "content-type": "application/json"})
    assert r.status_code == 400


# --- non-chat passthrough ---


def test_non_chat_get_passthrough(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: httpx.Response(200, json={"ok": True}))
    r = gw.client.get("/v1/something-else", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert tr.requests[0].url.path.endswith("/something-else")
    # gate headers are still applied to the upstream request
    assert tr.requests[0].headers["authorization"] == "Bearer public"

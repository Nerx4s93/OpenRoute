"""Integration tests for the gateway (main.py) through TestClient — NO real network.

The upstream is replaced by a ScriptedTransport (the router scripts responses),
proxy fetching is disabled, and retry sleeps are recorded in ``gw.sleeps``
instead of being awaited. Chat upstream always answers SSE (the opencode gate
forces streaming); callers may still ask for a non-stream answer, which the
gateway converts to a standard OpenAI non-stream ``chat.completion`` object.
"""

import gzip
import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

import gateway.main as main
import gateway.proxies as prox
from gateway.keys import hash_key
from gateway.reqlog import LogEntry
from starlette.requests import Request

from gw import TrackingStream

AUTH = {"Authorization": "Bearer test-key"}
SSE_HEADERS = {"content-type": "text/event-stream"}
FREE_MODEL = "deepseek-v4.1-flash-free"
TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-01$")
B3_RE = re.compile(r"^([0-9a-f]{32})-([0-9a-f]{16})-1-([0-9a-f]{16})$")

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
    assert sent.headers["user-agent"] == "opencode/latest/2.1.3/cli"
    assert sent.headers["accept"] == "*/*"
    sid = sent.headers["x-opencode-session"]
    for h in ("x-session-affinity", "x-session-id", "x-opencode-session-id"):
        assert sent.headers[h] == sid
    up = upstream_json(tr)
    assert up["model"] == FREE_MODEL
    assert up["stream"] is True
    assert up["stream_options"] == {"include_usage": True}
    assert up["max_completion_tokens"] == 32000
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


def test_gate_defaults_max_completion_tokens_but_keeps_caller_value(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions",
                       json=body(max_completion_tokens=4096), headers=AUTH)
    assert r.status_code == 200
    up = upstream_json(tr)
    assert up["max_completion_tokens"] == 4096  # never overwritten


def test_caller_user_field_never_forwarded(make_gateway):
    """The OpenAI ``user`` end-user identifier must not leak upstream: the
    real OpenCode body never carries it, and it is a stable caller identity."""
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions",
                       json=body(user="combain-user-42"), headers=AUTH)
    assert r.status_code == 200
    up = upstream_json(tr)
    assert "user" not in up


# --- upstream session identity ---


def test_gate_replaces_caller_session_id(make_gateway):
    gw = make_gateway(proxies=("http://proxy-a.invalid:8080",))
    tr = gw.script_upstream(lambda req: sse_response())
    sid = "ses_0123456789ab" + "A" * 14

    r = gw.client.post("/v1/chat/completions", json=body(),
                       headers={**AUTH, "x-opencode-session": sid})
    assert r.status_code == 200
    sent = tr.requests[0]
    persona = gw.personas.get_cached(
        prox.canonical_quota_key("http://proxy-a.invalid:8080"))
    assert persona is not None
    assert persona.session_id != sid
    assert main.opencode.is_session_id(persona.session_id)
    for name in main.opencode.SESSION_HEADER_NAMES:
        assert sent.headers[name] == persona.session_id


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


def test_gate_persona_shared_by_conversations_on_same_proxy(make_gateway):
    """Different conversations share the bucket's persona upstream.

    The client-side affinity guarantee (different fingerprints -> different
    session keys) still holds and is asserted at the resolution level.
    """
    gw = make_gateway(proxies=("http://proxy-a.invalid:8080",))
    tr = gw.script_upstream(lambda req: sse_response())
    a = body(messages=[{"role": "user", "content": "a"}])
    b = body(messages=[{"role": "user", "content": "b"}])
    assert gw.client.post("/v1/chat/completions", json=a,
                          headers=AUTH).status_code == 200
    assert gw.client.post("/v1/chat/completions", json=b,
                          headers=AUTH).status_code == 200

    sent_a = tr.requests[0].headers["x-opencode-session"]
    sent_b = tr.requests[1].headers["x-opencode-session"]
    assert sent_a == sent_b  # one persona for the bucket
    assert main.opencode.is_session_id(sent_a)

    # Affinity is still per conversation, not per persona.
    key = gw.keystore.authenticate("Bearer test-key")

    def resolved(payload):
        req = Request({"type": "http",
                       "headers": [(b"authorization", b"Bearer test-key")]})
        return main._resolve_session(req, key, json.dumps(payload).encode())

    first, second = resolved(a), resolved(b)
    assert first.key != second.key
    assert first.key.startswith("fp:") and second.key.startswith("fp:")


def test_gate_retry_uses_other_bucket_persona(make_gateway):
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
    sessions = [r.headers["x-opencode-session"] for r in tr.requests]
    assert all(main.opencode.is_session_id(s) for s in sessions)
    assert sessions[0] != sessions[1]  # A and B are different buckets
    # Every attempt is internally consistent across all four carriers.
    for req in tr.requests:
        values = {req.headers[name] for name in main.opencode.SESSION_HEADER_NAMES}
        assert len(values) == 1
    # A plain 500 is not a quota/ban verdict: neither bucket rotates.
    for url in ("http://proxy-a.invalid:8080", "http://proxy-b.invalid:8080"):
        persona = gw.personas.get_cached(prox.canonical_quota_key(url))
        assert persona is not None and persona.generation == 0


# --- persona headers ---


def test_gate_persona_headers_are_valid(make_gateway):
    gw = make_gateway(proxies=("http://proxy-a.invalid:8080",))
    tr = gw.script_upstream(lambda req: sse_response())

    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 200
    sent = tr.requests[0]
    project = sent.headers["x-opencode-project"]
    session = sent.headers["x-opencode-session"]
    traceparent = sent.headers["traceparent"]
    b3 = sent.headers["b3"]

    assert main.opencode.is_project_id(project)
    assert main.opencode.is_session_id(session)
    tp = TRACEPARENT_RE.fullmatch(traceparent)
    b3m = B3_RE.fullmatch(b3)
    assert tp is not None and b3m is not None
    assert tp.group(1) == b3m.group(1)  # same trace
    assert tp.group(2) == b3m.group(2)  # same span
    assert b3m.group(3) != b3m.group(2)  # separate parent span
    assert main.opencode.is_trace_id(tp.group(1))
    for name in main.opencode.SESSION_HEADER_NAMES:
        assert sent.headers[name] == session


def test_gate_persona_is_stable_and_span_changes_per_request(make_gateway):
    gw = make_gateway(proxies=("http://proxy-a.invalid:8080",))
    tr = gw.script_upstream(lambda req: sse_response())

    for _ in range(2):
        assert gw.client.post("/v1/chat/completions", json=body(),
                              headers=AUTH).status_code == 200
    first, second = tr.requests[0].headers, tr.requests[1].headers
    assert first["x-opencode-session"] == second["x-opencode-session"]
    assert first["x-opencode-project"] == second["x-opencode-project"]

    tp1 = TRACEPARENT_RE.fullmatch(first["traceparent"])
    tp2 = TRACEPARENT_RE.fullmatch(second["traceparent"])
    assert tp1 is not None and tp2 is not None
    assert tp1.group(1) == tp2.group(1)  # stable trace id
    assert tp1.group(2) != tp2.group(2)  # fresh span per request
    b31 = B3_RE.fullmatch(first["b3"])
    b32 = B3_RE.fullmatch(second["b3"])
    assert b31 is not None and b32 is not None
    assert b31.group(3) != b32.group(3)  # fresh B3 parent per request


def test_gate_persona_overrides_client_trace_and_project(make_gateway):
    gw = make_gateway(proxies=("http://proxy-a.invalid:8080",))
    tr = gw.script_upstream(lambda req: sse_response())
    caller = {
        **AUTH,
        "x-opencode-session": "ses_0123456789ab" + "D" * 14,
        "x-opencode-project": "client-project",
        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        "b3": "a" * 32 + "-" + "b" * 16 + "-1",
        "tracestate": "caller=1",
        "x-b3-traceid": "a" * 32,
        "x-b3-spanid": "b" * 16,
        "x-b3-sampled": "1",
        "x-b3-parentspanid": "c" * 16,
    }
    r = gw.client.post("/v1/chat/completions", json=body(), headers=caller)
    assert r.status_code == 200
    sent = tr.requests[0]
    persona = gw.personas.get_cached(
        prox.canonical_quota_key("http://proxy-a.invalid:8080"))
    assert persona is not None
    assert sent.headers["x-opencode-project"] == persona.project_id
    assert sent.headers["x-opencode-session"] == persona.session_id
    assert "a" * 32 not in sent.headers["traceparent"]
    assert sent.headers["traceparent"].endswith("-01")
    assert sent.headers["traceparent"].split("-")[1] == persona.trace_id
    assert sent.headers["b3"].split("-")[0] == persona.trace_id
    assert B3_RE.fullmatch(sent.headers["b3"]) is not None
    assert "tracestate" not in sent.headers
    for name in ("x-b3-traceid", "x-b3-spanid", "x-b3-sampled",
                 "x-b3-parentspanid"):
        assert name not in sent.headers


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


def test_affinity_status_exposes_host_only_groups(make_gateway):
    gw = make_gateway()
    gw.script_upstream(lambda req: sse_response())
    sid = "ses_0123456789ab" + "E" * 14
    r = gw.client.post("/v1/chat/completions", json=body(),
                       headers={**AUTH, "x-opencode-session": sid})
    assert r.status_code == 200

    aff = gw.affinity.status()
    assert aff["enabled"] is True
    assert aff["sessions_total"] == 1
    assert aff["sessions_bound"] == 1 and aff["sessions_unbound"] == 0
    assert len(aff["egress_groups"]) == 2
    bound = next(g for g in aff["egress_groups"] if g["sessions"] == 1)
    assert bound["proxy_count"] == 1
    assert bound["requests_1h"] == 1 and bound["tokens_1h"] == 8
    # Host-only snapshot: masked host:port entries, no scheme or credentials.
    assert bound["proxies"] == [bound["host"]]
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
               and not g.permanently_banned
               for g in gw.manager.egress_groups())
    assert gw.sleeps == []


# --- persona rotation ------------------------------------------------------


def test_generic_429_keeps_persona(make_gateway):
    url = "http://proxy-a.invalid:8080"
    gw = make_gateway(proxies=(url,))
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return sse_response()
        return httpx.Response(429, json={
            "error": {"type": "RateLimitError", "message": "provider rate"}})

    tr = gw.script_upstream(router)
    key = prox.canonical_quota_key(url)
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers=AUTH).status_code == 200
    before = gw.personas.get_cached(key)
    assert before is not None

    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 429
    assert len(tr.requests) == 2
    after = gw.personas.get_cached(key)
    assert after is not None
    assert after.generation == before.generation
    assert after.session_id == before.session_id


def test_free_usage_limit_rotates_persona(make_gateway):
    url = "http://proxy-a.invalid:8080"
    gw = make_gateway(proxies=(url,))
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return sse_response()
        return httpx.Response(429, json={
            "type": "error",
            "error": {"type": "FreeUsageLimitError", "message": "daily limit"},
        })

    gw.script_upstream(router)
    key = prox.canonical_quota_key(url)
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers=AUTH).status_code == 200
    before = gw.personas.get_cached(key)
    assert before is not None

    # The only bucket is parked after the 429; the client gets "no ready
    # proxy", but the persona must already be rotated.
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code in (429, 502, 503)
    after = gw.personas.get_cached(key)
    assert after is not None
    assert after.generation == before.generation + 1
    assert after.session_id != before.session_id
    assert after.project_id != before.project_id
    assert after.trace_id != before.trace_id


def test_abusive_traffic_403_rotates_persona(make_gateway):
    url = "http://proxy-a.invalid:8080"
    gw = make_gateway(proxies=(url,))
    calls = {"n": 0}

    def router(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return sse_response()
        return httpx.Response(403, content=b"Access denied: Abusive Traffic")

    gw.script_upstream(router)
    key = prox.canonical_quota_key(url)
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers=AUTH).status_code == 200
    before = gw.personas.get_cached(key)
    assert before is not None

    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code in (429, 502, 503)
    after = gw.personas.get_cached(key)
    assert after is not None
    assert after.generation == before.generation + 1
    assert after.session_id != before.session_id


def test_persona_disabled_forwards_caller_session(make_gateway):
    gw = make_gateway(proxies=("http://proxy-a.invalid:8080",),
                      settings_over={"persona_enabled": False})
    tr = gw.script_upstream(lambda req: sse_response())
    sid = "ses_0123456789ab" + "F" * 14
    traceparent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    b3 = "a" * 32 + "-" + "b" * 16 + "-1"
    caller = {
        **AUTH,
        "x-opencode-session": sid,
        "x-opencode-project": "client-project",
        "traceparent": traceparent,
        "b3": b3,
        "tracestate": "caller=1",
    }

    r = gw.client.post("/v1/chat/completions", json=body(), headers=caller)
    assert r.status_code == 200
    sent = tr.requests[0]
    assert sent.headers["x-opencode-project"] == "client-project"
    assert sent.headers["traceparent"] == traceparent
    assert sent.headers["b3"] == b3
    assert sent.headers["tracestate"] == "caller=1"
    for name in main.opencode.SESSION_HEADER_NAMES:
        assert sent.headers[name] == sid


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
    # Affinity off: this test targets the pool's cooldown round-robin, while
    # session scoring may legitimately keep preferring the other proxy.
    gw = make_gateway(settings_over={"affinity_enabled": False})
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
    assert a.banned_until == 0.0 and not a.permanently_banned
    a_proxy = a.proxies[0]
    assert a_proxy.errors == 0 and a_proxy.cooldown_until == 0.0  # no mark_failure

    state = gw.affinity._sessions[f"sid:{sid}"]
    assert gw.manager.group_of(state.proxy) is b
    assert b.proxies[0].requests == 1
    gw.flush_affinity()
    row = gw.affinity_binding(f"sid:{sid}")
    assert row is not None and row["proxy"] == state.proxy


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
    # A plain 401 is not a quota/ban verdict: no persona rotation.
    for url in (a.url, "http://proxy-b.invalid:8080"):
        persona = gw.personas.get_cached(prox.canonical_quota_key(url))
        assert persona is not None and persona.generation == 0


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
    # FreeTierError is not a quota/ban verdict: the persona is not rotated.
    chosen = next(p for p in gw.manager.proxies if p.requests == 1)
    persona = gw.personas.get_cached(prox.canonical_quota_key(chosen))
    assert persona is not None and persona.generation == 0


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
    assert a.permanently_banned is True
    assert a.banned_until == 0.0 and a.quota_until == 0.0
    assert a.proxies[0].errors == 0  # not a proxy failure
    state = gw.affinity._sessions[f"sid:{sid}"]
    assert gw.manager.group_of(state.proxy) is b
    row = next(p for p in gw.manager.status()["proxies"]
               if p["proxy"] == "proxy-a.invalid:8080")
    assert row["banned"] is True and row["banned_seconds_left"] == 0.0
    assert gw.manager.status()["egress_groups_banned"] == 1

    # A later request must never select the permanently banned bucket again.
    r2 = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r2.status_code == 200
    assert len(tr.requests) == 3  # A once, then B twice (no A retry)
    assert a.proxies[0].requests == 1
    assert b.proxies[0].requests == 2


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
    assert all(g.banned_until == 0.0 and not g.permanently_banned
               for g in gw.manager.egress_groups())



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
        "error: 500 for url 'http://svc:supersecret@px6-service:8000/proxies'"
    )
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == 503
    assert err["message"] == "No enabled proxies"
    assert "http://" not in err["message"]
    assert "supersecret" not in r.text and "px6-service" not in r.text
    assert tr.requests == []


def test_health_redacts_fetch_credentials(make_gateway):
    """last_proxies_fetch (public /health) strips userinfo."""
    gw = make_gateway(proxies=())
    gw.manager.last_fetch_status = (
        "error: 500 for url 'http://svc:supersecret@px6-service:8000/proxies'"
    )
    h = gw.client.get("/health").json()
    assert "supersecret" not in json.dumps(h)
    assert "http://***@px6-service:8000/proxies" in h["last_proxies_fetch"]


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


# --- removed infrastructure routes ---


def test_keys_cannot_access_admin_infrastructure(make_gateway):
    gw = make_gateway()
    gw.script_upstream(lambda req: sse_response())
    for path in ("/admin/status", "/admin/logs"):
        assert gw.direct.get(path, headers=AUTH).status_code == 404
        assert gw.client.get(path, headers=AUTH).status_code == 404


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
    """Strict allowlist: the upstream request carries EXACTLY the header set
    of the real OpenCode client — nothing the caller sent is merged in."""
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: sse_response())
    r = gw.client.post(
        "/v1/chat/completions", json=body(),
        headers={**AUTH, "x-forwarded-for": "9.9.9.9", "cookie": "session=1",
                 "x-api-key": "leak-me", "x-real-ip": "8.8.8.8",
                 "x-stainless-lang": "python", "x-stainless-os": "Linux",
                 "x-stainless-runtime": "CPython",
                 "x-request-id": "req-1", "baggage": "session=user-42",
                 "origin": "https://combain.example",
                 "referer": "https://combain.example/chat",
                 "accept-language": "ru-RU", "sec-fetch-site": "same-origin",
                 "sec-ch-ua": '"Chromium"', "x-custom-user-id": "user-42"},
    )
    assert r.status_code == 200
    sent = tr.requests[0]
    assert sent.headers["authorization"] == "Bearer public"  # client key never forwarded
    for h in ("x-forwarded-for", "cookie", "x-api-key", "x-real-ip",
              "x-stainless-lang", "x-stainless-os", "x-stainless-runtime",
              "x-request-id", "baggage", "origin", "referer",
              "accept-language", "sec-fetch-site", "sec-ch-ua",
              "x-custom-user-id"):
        assert h not in sent.headers
    # The exact allowlist: nothing beyond the real client's header set and
    # the transport headers httpx itself manages (host/accept-encoding/
    # connection/content-length) ever reaches the upstream.
    gate_and_trace = {
        "authorization", "content-type", "accept", "user-agent",
        "x-opencode-client", "x-opencode-project", "x-opencode-session",
        "x-opencode-session-id", "x-session-affinity", "x-session-id",
        "traceparent", "b3",
    }
    transport_managed = {
        "host", "accept-encoding", "connection", "content-length",
    }
    got = {k.lower() for k in sent.headers.keys()}
    assert got - transport_managed == gate_and_trace
    # ...and the identity values are the persona's, not the caller's.
    persona = gw.personas.get_cached(
        prox.canonical_quota_key("http://proxy-a.invalid:8080"))
    assert persona is not None
    assert sent.headers["x-opencode-session"] == persona.session_id
    assert sent.headers["x-opencode-project"] == persona.project_id


def test_chat_query_params_never_forwarded(make_gateway):
    """Real OpenCode calls chat/completions with NO query string: caller
    params (e.g. "?api_key=...") must not leak into the upstream URL."""
    gw = make_gateway()
    seen = {}

    def router(req):
        seen["url"] = str(req.url)
        return sse_response()

    gw.script_upstream(router)
    r = gw.client.post("/v1/chat/completions?api_key=sk-leak&a=1&a=2&b=3",
                       json=body(), headers=AUTH)
    assert r.status_code == 200
    assert "?" not in seen["url"]
    assert seen["url"].endswith("/chat/completions")


def test_nonchat_query_params_still_forwarded(make_gateway):
    """Non-chat passthrough keeps its query params (only chat is normalised
    to the real OpenCode wire shape)."""
    gw = make_gateway()
    seen = {}

    def router(req):
        seen["url"] = str(req.url)
        return ok_response()

    gw.script_upstream(router)
    r = gw.client.get("/v1/probe?a=1&a=2&b=3", headers=AUTH)
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
        for row in gw.call(gw.reqlog.recent_async, 20):
            if pred(row):
                return row
        time.sleep(0.05)
    raise AssertionError("log entry never appeared in reqlog")


def test_request_log_and_usage_persisted(make_gateway):
    gw = make_gateway()
    gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 200

    row = _wait_log_entry(gw, lambda x: x["path"] == "chat/completions")
    assert row["key_id"] == "default" and row["status"] == 200
    assert row["method"] == "POST" and row["model"] == FREE_MODEL
    assert row["account"] == "proxy-a.invalid:8080"  # masked proxy
    assert row["prompt_tokens"] == 6 and row["completion_tokens"] == 2
    usage = gw.call(gw.reqlog.summary_async)
    assert usage["total"]["requests"] >= 1


def test_startup_creates_storage_backed_reqlog(make_gateway):
    """No injected facade: the lifespan builds one on the shared storage client
    and the request path writes through to ``FakeStorage.log``."""
    gw = make_gateway(inject_reqlog=False)
    assert gw.reqlog is not None and gw.reqlog is main.reqlog
    assert gw.reqlog._storage is gw.storage

    gw.script_upstream(lambda req: sse_response())
    r = gw.client.post("/v1/chat/completions", json=body(), headers=AUTH)
    assert r.status_code == 200

    row = _wait_log_entry(gw, lambda x: x["path"] == "chat/completions")
    assert row["key_id"] == "default" and row["account"] == "proxy-a.invalid:8080"
    assert any(
        entry["path"] == "chat/completions" and entry["key_id"] == "default"
        for entry in gw.storage.log
    )


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


def test_internal_proxies_refresh_forces_cache_bust(make_gateway, monkeypatch):
    gw = make_gateway()
    calls: list[tuple[bool, bool]] = []

    async def fake_refresh(
        bust_cache: bool = False, enable_new: bool = False
    ) -> bool:
        calls.append((bust_cache, enable_new))
        return True

    monkeypatch.setattr(gw.manager, "refresh_proxies", fake_refresh)
    h = {"X-Internal-Token": "test-internal-token"}

    assert gw.client.post("/internal/proxies/refresh").status_code == 404
    assert gw.internal.post("/internal/proxies/refresh").status_code == 403
    response = gw.internal.post("/internal/proxies/refresh", headers=h)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "Proxies refreshed"}
    assert calls == [(True, True)]


def test_internal_proxies_refresh_failure_is_openai_error(make_gateway, monkeypatch):
    gw = make_gateway()

    async def failing_refresh(
        bust_cache: bool = False, enable_new: bool = False
    ) -> bool:
        return False

    monkeypatch.setattr(gw.manager, "refresh_proxies", failing_refresh)
    response = gw.internal.post(
        "/internal/proxies/refresh",
        headers={"X-Internal-Token": "test-internal-token"},
    )
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Proxy refresh failed"


def test_internal_px6_account_countries_and_price(make_gateway, monkeypatch):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}
    calls: list[tuple[str, tuple, dict]] = []

    async def fake_call(action: str, *args, **kwargs):
        calls.append((action, args, kwargs))
        if action == "account":
            return {"user_id": "u1", "balance": 100.5, "currency": "RUB"}
        if action == "countries":
            return {"version": args[0], "countries": ["de", "ru"]}
        if action == "price":
            return {"price": 12.5, "price_single": 12.5, "period": args[2],
                    "count": args[1], "version": args[0], "currency": "RUB"}
        raise AssertionError(action)

    monkeypatch.setattr(main, "_px6_call", fake_call)

    account = gw.internal.get("/internal/px6/account", headers=h)
    assert account.status_code == 200
    assert account.json()["balance"] == 100.5
    countries = gw.internal.get("/internal/px6/countries?version=4", headers=h)
    assert countries.status_code == 200
    assert countries.json()["countries"] == ["de", "ru"]
    price = gw.internal.get(
        "/internal/px6/price?version=6&count=3&period=7", headers=h,
    )
    assert price.status_code == 200
    assert price.json()["count"] == 3
    assert calls == [
        ("account", (), {}),
        ("countries", (4,), {}),
        ("price", (6, 3, 7), {}),
    ]

    bad = gw.internal.get("/internal/px6/countries?version=5", headers=h)
    assert bad.status_code == 400
    assert "Unsupported proxy version" in bad.json()["error"]["message"]


def test_internal_px6_buy_refreshes_and_keeps_success_on_refresh_failure(
    make_gateway, monkeypatch,
):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}
    calls: list[tuple] = []
    refresh_calls: list[tuple[bool, bool]] = []

    async def fake_call(action: str, *args, **kwargs):
        calls.append((action, args, kwargs))
        return {
            "order_id": "o1", "count": 2, "price": 25.0,
            "price_single": 12.5, "period": 30, "country": "ru",
            "version": 6, "currency": "RUB", "balance": 75.0,
        }

    async def fake_refresh(
        bust_cache: bool = False, enable_new: bool = False
    ) -> bool:
        refresh_calls.append((bust_cache, enable_new))
        return True

    monkeypatch.setattr(main, "_px6_call", fake_call)
    monkeypatch.setattr(gw.manager, "refresh_proxies", fake_refresh)

    response = gw.internal.post(
        "/internal/px6/buy",
        headers=h,
        json={"count": 2, "period": 30, "country": "RU", "version": 6,
              "descr": "prod"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["refresh_ok"] is True
    assert body["order_id"] == "o1" and body["balance"] == 75.0
    assert refresh_calls == [(True, True)]
    assert calls == [("buy", (), {"payload": {
        "count": 2, "period": 30, "country": "ru", "version": 6,
        "descr": "prod", "auto_prolong": False,
    }})]

    # A failed follow-up refresh must not hide a completed purchase.
    refresh_calls.clear()

    async def failing_refresh(
        bust_cache: bool = False, enable_new: bool = False
    ) -> bool:
        refresh_calls.append((bust_cache, enable_new))
        return False

    monkeypatch.setattr(gw.manager, "refresh_proxies", failing_refresh)
    second = gw.internal.post(
        "/internal/px6/buy",
        headers=h,
        json={"count": 1, "period": 3, "country": "de", "version": 4},
    )
    assert second.status_code == 200
    assert second.json()["ok"] is True
    assert second.json()["refresh_ok"] is False
    assert "purchased" in second.json()["message"].lower()
    assert refresh_calls == [(True, True)]


def test_internal_px6_error_is_passed_through(make_gateway, monkeypatch):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}

    class FakePx6Client:
        def __init__(self, settings):
            self.settings = settings

        async def aclose(self):
            return None

        async def buy(self, payload):
            raise main.Px6ServiceError(402, "Insufficient px6.net balance")

    monkeypatch.setattr(main, "Px6ServiceClient", FakePx6Client)
    response = gw.internal.post(
        "/internal/px6/buy",
        headers=h,
        json={"count": 1, "period": 30, "country": "ru", "version": 6},
    )
    assert response.status_code == 402
    assert response.json()["error"]["message"] == "Insufficient px6.net balance"


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


def test_internal_period_filters_usage(make_gateway, monkeypatch):
    """?period selects UTC windows: rolling 1d/3d/7d/30d/90d and the legacy
    day/week/month calendar aliases; unknown values behave like all time.
    The recent-requests feed stays unfiltered."""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2026, 5, 20, 15, 30, tzinfo=timezone.utc)
            return fixed.astimezone(tz)

    monkeypatch.setattr(main, "datetime", FrozenDateTime)
    gw = make_gateway()

    def ts(*args) -> float:
        return datetime(*args, tzinfo=timezone.utc).timestamp()

    async def seed():
        # Previous month.
        old = LogEntry(
            ts=ts(2026, 4, 15, 12, 0), key_id="old", method="POST",
            path="chat/completions", model=FREE_MODEL, status=200, latency_ms=10.0,
            stream=True, account="1.2.3.4:8080", prompt_tokens=100,
            completion_tokens=0, error=None,
        )
        # This month, but before the current week.
        month = LogEntry(
            ts=ts(2026, 5, 10, 12, 0), key_id="month", method="POST",
            path="chat/completions", model=FREE_MODEL, status=200, latency_ms=10.0,
            stream=True, account="2.3.4.5:8080", prompt_tokens=20,
            completion_tokens=0, error=None,
        )
        # This week, but yesterday: less than 24h before the frozen now, so a
        # rolling 24h window would include it while today's window must not.
        prev_day = LogEntry(
            ts=ts(2026, 5, 19, 20, 0), key_id="prev_day", method="POST",
            path="chat/completions", model=FREE_MODEL, status=200, latency_ms=10.0,
            stream=True, account="3.4.5.6:8080", prompt_tokens=5,
            completion_tokens=0, error=None,
        )
        # Today, exactly at the UTC midnight boundary.
        day = LogEntry(
            ts=ts(2026, 5, 20, 0, 0), key_id="day", method="POST",
            path="chat/completions", model=FREE_MODEL, status=200, latency_ms=20.0,
            stream=True, account="5.6.7.8:8080", prompt_tokens=7,
            completion_tokens=3, error=None,
        )
        for entry in (old, month, prev_day, day):
            await gw.reqlog.log(entry)

    gw._gw_lifespan_client.portal.call(seed)
    h = {"X-Internal-Token": "test-internal-token"}

    all_data = gw.internal.get("/internal/proxies-data?period=all", headers=h).json()
    assert all_data["usage"]["total"]["requests"] == 4
    assert len(all_data["recent"]) == 4

    day = gw.internal.get("/internal/proxies-data?period=day", headers=h).json()
    assert day["usage"]["total"]["requests"] == 1
    assert day["usage"]["total"]["tokens"] == 10
    assert set(day["usage_by_proxy"]) == {"5.6.7.8:8080"}
    # The activity log is not a spending aggregate: it still shows every row.
    assert len(day["recent"]) == 4

    week = gw.internal.get("/internal/proxies-data?period=week", headers=h).json()
    assert week["usage"]["total"]["requests"] == 2
    assert week["usage"]["total"]["tokens"] == 15

    month = gw.internal.get("/internal/proxies-data?period=month", headers=h).json()
    assert month["usage"]["total"]["requests"] == 3
    assert month["usage"]["total"]["tokens"] == 35

    for period, requests, tokens in (
        ("1d", 1, 10),
        ("3d", 2, 15),
        ("7d", 2, 15),
        ("30d", 3, 35),
        ("90d", 4, 135),
    ):
        rolling = gw.internal.get(
            f"/internal/proxies-data?period={period}", headers=h
        ).json()
        assert rolling["usage"]["total"]["requests"] == requests
        assert rolling["usage"]["total"]["tokens"] == tokens

    unknown = gw.internal.get("/internal/proxies-data?period=year", headers=h).json()
    assert unknown["usage"]["total"]["requests"] == 4

    keys_day = gw.internal.get("/internal/keys-data?period=day", headers=h).json()
    assert keys_day["usage"]["total"]["requests"] == 1
    assert set(keys_day["usage"]) == {"day", "total"}

    keys_7d = gw.internal.get("/internal/keys-data?period=7d", headers=h).json()
    assert keys_7d["usage"]["total"]["requests"] == 2
    assert set(keys_7d["usage"]) == {"prev_day", "day", "total"}


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


def test_internal_proxies_set_and_delete_removed(make_gateway):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}

    r = gw.internal.post("/internal/proxies/set",
                         json={"proxy": "http://proxy-a.invalid:8080", "enabled": False},
                         headers=h)
    assert r.status_code == 200
    row = next(p for p in gw.internal.get("/internal/proxies-data", headers=h).json()["status"]["proxies"]
               if p["real_proxy"] == "http://proxy-a.invalid:8080")
    assert row["enabled"] is False and row["manual"] is True

    # Proxy deletion is out of scope: the internal endpoint is gone.
    r2 = gw.internal.post("/internal/proxies/delete",
                          json={"proxy": "http://proxy-b.invalid:8080"}, headers=h)
    assert r2.status_code == 404
    assert any(p.url == "http://proxy-b.invalid:8080" for p in gw.manager.proxies)


def test_internal_disable_invalidates_affinity(make_gateway):
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
    row = gw.affinity_binding(f"sid:{sid}")
    assert row is not None and row["proxy"] is None  # persisted as proxy = NULL
    assert row["created_at"] is not None  # row/created_at preserved

    # The next turn leaves the disabled exit and binds to the remaining one.
    assert gw.client.post("/v1/chat/completions", json=body(), headers=auth).status_code == 200
    assert state.proxy == other


def test_internal_keys_add_set_delete_and_persist(make_gateway):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}

    r = gw.internal.post("/internal/keys/add",
                         json={"key_id": "n1", "rpm": 5},
                         headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True
    issued = r.json()
    secret = issued["key"]
    assert issued["key_id"] == "n1"
    assert secret.startswith("openroute-") and len(secret) == 58

    # the new key works immediately
    gw.script_upstream(lambda req: sse_response())
    headers = {"Authorization": f"Bearer {secret}"}
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers=headers).status_code == 200

    data = gw.internal.get("/internal/keys-data", headers=h).json()
    row = next(k for k in data["keys"] if k["key_id"] == "n1")
    assert "admin" not in row and "name" not in row and row["rpm"] == 5
    assert row["key_hash"] == hash_key(secret)
    assert secret not in json.dumps(data)

    r2 = gw.internal.post("/internal/keys/set",
                          json={"key_hash": row["key_hash"], "enabled": False}, headers=h)
    assert r2.status_code == 200
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers=headers).status_code == 401

    r3 = gw.internal.post("/internal/keys/delete", json={"key_hash": row["key_hash"]}, headers=h)
    assert r3.status_code == 200


def test_internal_keys_rotate_invalidates_old_and_preserves_config(make_gateway):
    gw = make_gateway(keys=[{
        "key": "openroute-old-secret",
        "key_id": "r1",
        "rpm": 7,
        "tpm": 900,
        "models": [FREE_MODEL],
    }])
    h = {"X-Internal-Token": "test-internal-token"}
    old_headers = {"Authorization": "Bearer openroute-old-secret"}

    gw.script_upstream(lambda req: sse_response())
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers=old_headers).status_code == 200

    before = gw.internal.get("/internal/keys-data", headers=h).json()["keys"][0]
    r = gw.internal.post("/internal/keys/rotate",
                         json={"key_hash": before["key_hash"]}, headers=h)
    assert r.status_code == 200, r.text
    rotated = r.json()
    secret = rotated["key"]
    assert rotated["key_id"] == "r1"
    assert secret.startswith("openroute-") and secret != "openroute-old-secret"

    # old secret is dead immediately; the new one works with the same config
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers=old_headers).status_code == 401
    new_headers = {"Authorization": f"Bearer {secret}"}
    assert gw.client.post("/v1/chat/completions", json=body(),
                          headers=new_headers).status_code == 200

    after = gw.internal.get("/internal/keys-data", headers=h).json()["keys"][0]
    assert after["key_hash"] == hash_key(secret)
    assert after["key_hash"] != before["key_hash"]
    assert after["key_id"] == "r1"
    assert after["rpm"] == 7 and after["tpm"] == 900
    assert after["models"] == [FREE_MODEL]
    assert after["enabled"] is True
    assert secret not in json.dumps(after)

    missing = gw.internal.post("/internal/keys/rotate",
                               json={"key_hash": "no-such-hash"}, headers=h)
    assert missing.status_code == 404
    empty = gw.internal.post("/internal/keys/rotate", json={}, headers=h)
    assert empty.status_code == 400


def test_internal_bad_json_body_400(make_gateway):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}
    r = gw.internal.post("/internal/keys/add", content=b"not-json",
                         headers={**h, "content-type": "application/json"})
    assert r.status_code == 400


# --- internal API: token statistics ----------------------------------------


def _today_utc() -> datetime:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)


def test_internal_stats_data_auth_and_days_clamp(make_gateway):
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}

    assert gw.client.get("/internal/stats-data").status_code == 404  # edge hides it
    assert gw.internal.get("/internal/stats-data").status_code == 403
    assert gw.internal.get(
        "/internal/stats-data", headers={"X-Internal-Token": "nope"}
    ).status_code == 403

    one = gw.internal.get("/internal/stats-data?days=0", headers=h).json()
    assert one["days"] == 1
    assert len(one["dates"]) == 1
    assert len(one["series_total"]["tokens"]) == 1
    assert one["dates"][-1] == _today_utc().strftime("%Y-%m-%d")
    assert one["totals"] == {
        "tokens_total": 0, "requests_total": 0, "errors_total": 0,
    }

    big = gw.internal.get("/internal/stats-data?days=9999", headers=h).json()
    assert big["days"] == 365
    assert len(big["dates"]) == 365
    assert big["dates"] == sorted(big["dates"])
    assert big["dates"][-1] == _today_utc().strftime("%Y-%m-%d")
    assert len(big["series_total"]["requests"]) == 365
    # No requests in the window -> no proxies leak in from the pool/history.
    assert big["proxies"] == []
    assert big["series_by_proxy"] == {}


def test_internal_stats_data_totals_and_archived(make_gateway):
    """Per-proxy all-time tokens, archived accounts marked out of the pool,
    and zero-filled day series aligned to ``dates``."""
    gw = make_gateway()
    today = _today_utc()

    def at(days_ago: int) -> float:
        return (today - timedelta(days=days_ago)).timestamp() + 12 * 3600

    prox = "proxy-a.invalid:8080"
    archived = "9.9.9.9:9000"

    async def seed():
        rows = [
            LogEntry(ts=at(4), key_id="k1", method="POST",
                     path="chat/completions", model=FREE_MODEL, status=200,
                     latency_ms=1.0, stream=True, account=prox,
                     prompt_tokens=100, completion_tokens=0, error=None),
            LogEntry(ts=at(0), key_id="k1", method="POST",
                     path="chat/completions", model=FREE_MODEL, status=200,
                     latency_ms=1.0, stream=True, account=prox,
                     prompt_tokens=7, completion_tokens=3, error=None),
            LogEntry(ts=at(0), key_id="k1", method="POST",
                     path="chat/completions", model=FREE_MODEL, status=500,
                     latency_ms=1.0, stream=True, account=archived,
                     prompt_tokens=5, completion_tokens=0, error="boom"),
        ]
        for row in rows:
            await gw.reqlog.log(row)

    gw._gw_lifespan_client.portal.call(seed)
    h = {"X-Internal-Token": "test-internal-token"}
    data = gw.internal.get("/internal/stats-data?days=7", headers=h).json()

    assert data["days"] == 7
    assert len(data["dates"]) == 7
    assert data["dates"][-1] == today.strftime("%Y-%m-%d")
    assert data["totals"] == {
        "tokens_total": 115,
        "requests_total": 3,
        "errors_total": 1,
    }

    by_name = {row["proxy"]: row for row in data["proxies"]}
    assert set(by_name) == {prox, archived}
    # In-pool proxies first, then archived; each group by tokens_total DESC.
    assert [row["proxy"] for row in data["proxies"]] == [prox, archived]

    a = by_name[prox]
    assert a["in_pool"] is True
    assert a["tokens_total"] == 110
    assert a["requests_total"] == 2 and a["errors_total"] == 0

    old = by_name[archived]
    assert old["in_pool"] is False
    assert old["tokens_total"] == 5
    assert old["requests_total"] == 1 and old["errors_total"] == 1

    assert set(data["series_by_proxy"]) == set(by_name)
    for series in data["series_by_proxy"].values():
        assert [len(series[key]) for key in ("tokens", "requests", "errors")] == [7, 7, 7]

    total = data["series_total"]
    assert len(total["tokens"]) == 7
    assert total["tokens"][-1] == 15 and total["requests"][-1] == 2
    assert total["errors"][-1] == 1
    assert total["tokens"][2] == 100  # 4 days ago (index 2 in a 7-day window)
    assert sum(total["tokens"]) == 115

    # Windows used by the Proxies page: rolling days and all time.
    today_str = today.strftime("%Y-%m-%d")
    day = gw.internal.get("/internal/stats-data?period=day", headers=h).json()
    assert day["dates"] == [today_str]
    assert day["series_total"] == {
        "tokens": [15], "requests": [2], "errors": [1],
    }
    day_by_name = {row["proxy"]: row for row in day["proxies"]}
    assert set(day_by_name) == {prox, archived}
    assert day_by_name[prox]["tokens_total"] == 10
    assert day_by_name[archived]["tokens_total"] == 5
    assert set(day["series_by_proxy"]) == {prox, archived}

    for period, count in (
        ("1d", 1), ("3d", 3), ("7d", 7), ("30d", 30), ("90d", 90),
    ):
        window = gw.internal.get(
            f"/internal/stats-data?period={period}", headers=h
        ).json()
        assert window["days"] == count
        assert len(window["dates"]) == count
        assert window["dates"][-1] == today_str
        assert sum(window["series_total"]["tokens"]) == (
            115 if count >= 7 else 15
        )

    all_time = gw.internal.get(
        "/internal/stats-data?period=all", headers=h
    ).json()
    assert all_time["days"] == 5
    assert all_time["dates"] == [
        (today - timedelta(days=4)).strftime("%Y-%m-%d"),
        (today - timedelta(days=3)).strftime("%Y-%m-%d"),
        (today - timedelta(days=2)).strftime("%Y-%m-%d"),
        (today - timedelta(days=1)).strftime("%Y-%m-%d"),
        today.strftime("%Y-%m-%d"),
    ]
    assert all_time["series_total"] == {
        "tokens": [100, 0, 0, 0, 15],
        "requests": [1, 0, 0, 0, 2],
        "errors": [0, 0, 0, 0, 1],
    }


def test_internal_stats_data_scopes_proxies_to_selected_period(make_gateway):
    """A proxy used only before the window must not appear or affect totals."""
    gw = make_gateway()
    today = _today_utc()
    current = "proxy-a.invalid:8080"
    old = "7.7.7.7:7000"

    async def seed():
        rows = [
            LogEntry(ts=(today - timedelta(days=10)).timestamp() + 3600,
                     key_id="k1", method="POST", path="chat/completions",
                     model=FREE_MODEL, status=200, latency_ms=1.0, stream=True,
                     account=old, prompt_tokens=100, completion_tokens=0,
                     error=None),
            LogEntry(ts=today.timestamp() + 3600, key_id="k1", method="POST",
                     path="chat/completions", model=FREE_MODEL, status=200,
                     latency_ms=1.0, stream=True, account=current,
                     prompt_tokens=7, completion_tokens=3, error=None),
        ]
        for row in rows:
            await gw.reqlog.log(row)

    gw._gw_lifespan_client.portal.call(seed)
    h = {"X-Internal-Token": "test-internal-token"}

    day = gw.internal.get("/internal/stats-data?period=1d", headers=h).json()
    assert [row["proxy"] for row in day["proxies"]] == [current]
    assert day["proxies"][0]["tokens_total"] == 10
    assert day["totals"]["tokens_total"] == 10
    assert set(day["series_by_proxy"]) == {current}
    assert old not in day["series_by_proxy"]

    all_time = gw.internal.get(
        "/internal/stats-data?period=all", headers=h
    ).json()
    by_name = {row["proxy"]: row for row in all_time["proxies"]}
    assert set(by_name) == {current, old}
    # In-pool first even though the archived proxy spent more.
    assert [row["proxy"] for row in all_time["proxies"]] == [current, old]
    assert by_name[current]["in_pool"] is True
    assert by_name[current]["tokens_total"] == 10
    assert by_name[old]["in_pool"] is False
    assert by_name[old]["tokens_total"] == 100


def test_internal_stats_data_fail_soft(make_gateway, monkeypatch):
    """Unavailable stores must not turn the stats endpoint into a 500."""
    gw = make_gateway()

    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(gw.reqlog, "proxy_stats_async", boom)
    monkeypatch.setattr(gw.reqlog, "daily_by_account_async", boom)
    r = gw.internal.get("/internal/stats-data?days=3",
                        headers={"X-Internal-Token": "test-internal-token"})
    assert r.status_code == 200
    data = r.json()
    assert data["days"] == 3
    assert data["totals"]["tokens_total"] == 0
    assert data["series_total"] == {
        "tokens": [0, 0, 0], "requests": [0, 0, 0], "errors": [0, 0, 0]}


def test_internal_proxies_data_adds_lifetime_stats(make_gateway):
    """The SSE snapshot keeps the old keys and adds lifetime counters; a pool
    proxy with no requests gets a zero record."""
    gw = make_gateway()
    h = {"X-Internal-Token": "test-internal-token"}

    first = gw.internal.get("/internal/proxies-data", headers=h).json()
    assert set(first) >= {"status", "usage", "usage_by_proxy", "recent",
                          "usage_by_proxy_lifetime"}
    assert first["usage"]["total"]["requests"] == 0
    assert first["usage_by_proxy"] == {}
    assert first["recent"] == []
    assert len(first["status"]["proxies"]) == 2
    for row in first["status"]["proxies"]:
        assert first["usage_by_proxy_lifetime"][row["proxy"]] == {
            "requests": 0, "errors": 0, "tokens": 0,
        }


def test_internal_proxies_data_lifetime_tokens(make_gateway):
    """Lifetime counters follow request_log rows by masked proxy name."""
    gw = make_gateway()

    async def seed():
        await gw.reqlog.log(LogEntry(
            ts=time.time(), key_id="k1", method="POST",
            path="chat/completions", model=FREE_MODEL, status=200,
            latency_ms=1.0, stream=True, account="proxy-a.invalid:8080",
            prompt_tokens=4, completion_tokens=6, error=None))

    gw._gw_lifespan_client.portal.call(seed)
    data = gw.internal.get("/internal/proxies-data",
                           headers={"X-Internal-Token": "test-internal-token"}).json()
    rec = data["usage_by_proxy_lifetime"]["proxy-a.invalid:8080"]
    assert rec == {"requests": 1, "errors": 0, "tokens": 10}
    other = data["usage_by_proxy_lifetime"]["proxy-b.invalid:8080"]
    assert other == {"requests": 0, "errors": 0, "tokens": 0}


# --- non-chat passthrough ---


def test_non_chat_get_passthrough(make_gateway):
    gw = make_gateway()
    tr = gw.script_upstream(lambda req: httpx.Response(200, json={"ok": True}))
    r = gw.client.get("/v1/something-else", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert tr.requests[0].url.path.endswith("/something-else")
    # gate headers are still applied to the upstream request
    assert tr.requests[0].headers["authorization"] == "Bearer public"

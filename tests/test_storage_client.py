"""Unit tests for :mod:`gateway.storage` (no network, no database).

Every test injects an ``httpx.MockTransport``: the client is exercised over its
real request-building/serialisation path while responses stay in memory. A
separate error matrix asserts the status -> exception mapping and that neither
response bodies nor secrets leak into exception messages.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from gateway.reqlog import LogEntry
from gateway.storage import (
    StorageAPIError,
    StorageClient,
    StorageConflict,
    StorageNotFound,
    StorageUnavailable,
)

BASE = "http://storage.test"
SECRET = "storage-secret-DO-NOT-LEAK"
SECRET_BODY = "upstream-body-DO-NOT-LEAK"

PROXY = {
    "proxy": "http://proxy-a.invalid:8080",
    "enabled": True,
    "quota_until": 0.0,
    "permanently_banned": False,
    "listed": True,
}
KEY = {
    "key_hash": "a" * 64,
    "key_id": "k1",
    "rpm": 60,
    "tpm": None,
    "models": ["deepseek-v4.1-flash-free"],
    "enabled": True,
}
BINDING = {
    "session_key": "sid:ses_abc",
    "proxy": PROXY["proxy"],
    "created_at": 10.0,
    "last_used_at": 20.0,
}
PERSONA = {
    "quota_key": "proxy-a.invalid:8080",
    "session_id": "ses_abc",
    "project_id": "b" * 40,
    "trace_id": "c" * 32,
    "created_at": 30.0,
    "generation": 3,
}
ENTRY = {
    "ts": 1779235200.0,
    "key_id": "k1",
    "method": "POST",
    "path": "chat/completions",
    "model": "deepseek-v4.1-flash-free",
    "status": 200,
    "latency_ms": 123.4,
    "stream": True,
    "account": "1.2.3.4:8080",
    "prompt_tokens": 10,
    "completion_tokens": 5,
    "error": None,
}


def _json(request: httpx.Request):
    return json.loads(request.content)


def _ok(request: httpx.Request, payload) -> httpx.Response:
    return httpx.Response(200, json=payload, request=request)


def _client(handler, **kwargs) -> tuple[StorageClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return (
        StorageClient(
            BASE, transport=httpx.MockTransport(recording), **kwargs
        ),
        calls,
    )


# --- health -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_probes_health_then_pool():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/health":
            return _ok(request, {"status": "ok", "database": "ok"})
        if request.url.path == "/proxies":
            return _ok(request, {"proxies": [PROXY]})
        return httpx.Response(404, request=request)

    client = StorageClient(BASE, transport=httpx.MockTransport(handler))
    info = await client.check()
    await client.aclose()

    assert info == {"status": "ok", "database": "ok", "proxies": 1}
    assert [request.method for request in calls] == ["GET", "GET"]
    assert [request.url.path for request in calls] == ["/health", "/proxies"]

    assert calls[1].url.params["listed"] == "true"


# --- proxies ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_methods_paths_payloads_and_return_values():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/proxies/sync":
            return _ok(request, {"added": 1, "proxies": [PROXY]})
        if request.url.path == "/proxies":
            return _ok(request, {"proxies": [PROXY]})
        if request.url.path == "/proxies/set-enabled":
            return _ok(
                request,
                {"proxy": PROXY["proxy"], "enabled": True, "updated": True},
            )
        if request.url.path == "/proxies/parking":
            return _ok(request, {"updated": 2})
        return httpx.Response(404, request=request)

    client, calls = _client(handler)
    synced = await client.sync_proxies(
        ["http://proxy-a.invalid:8080", "http://proxy-b.invalid:8080"],
        enable_new=True,
    )
    assert synced == {"added": 1, "proxies": [PROXY]}

    assert await client.list_proxies() == [PROXY]
    assert await client.list_proxies(listed=True) == [PROXY]
    assert await client.list_proxies(listed=False) == [PROXY]

    assert await client.set_proxy_enabled(PROXY["proxy"], True) is True
    assert await client.save_parking(
        [PROXY["proxy"]], quota_until=1779321600.0, permanently_banned=False,
    ) == 2
    await client.aclose()

    assert [(request.method, request.url.path) for request in calls] == [
        ("POST", "/proxies/sync"),
        ("GET", "/proxies"),
        ("GET", "/proxies"),
        ("GET", "/proxies"),
        ("POST", "/proxies/set-enabled"),
        ("POST", "/proxies/parking"),
    ]
    assert _json(calls[0]) == {
        "proxies": ["http://proxy-a.invalid:8080",
                    "http://proxy-b.invalid:8080"],
        "enable_new": True,
    }
    assert "listed" not in calls[1].url.params
    assert calls[2].url.params["listed"] == "true"
    assert calls[3].url.params["listed"] == "false"
    assert _json(calls[4]) == {"proxy": PROXY["proxy"], "enabled": True}
    assert _json(calls[5]) == {
        "proxies": [PROXY["proxy"]],
        "quota_until": 1779321600.0,
        "permanently_banned": False,
    }


# --- keys -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_methods_paths_payloads_and_return_values():
    new_hash = "d" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/keys" and request.method == "GET":
            return _ok(request, {"keys": [KEY]})
        if path == "/keys" and request.method == "POST":
            return _ok(request, {"created": True})
        if path == "/keys/rotate":
            return _ok(
                request,
                {"rotated": True, "already_rotated": False, "key_id": "k1"},
            )
        if path == "/keys/set-enabled":
            return _ok(
                request,
                {"key_hash": KEY["key_hash"], "enabled": False, "updated": True},
            )
        if path == f"/keys/{KEY['key_hash']}":
            return _ok(
                request, {"key_hash": KEY["key_hash"], "deleted": True}
            )
        return httpx.Response(404, request=request)

    client, calls = _client(handler)
    assert await client.list_keys() == [KEY]
    assert await client.add_key(
        KEY["key_hash"], "k1", rpm=60, tpm=None,
        models=["deepseek-v4.1-flash-free"],
    ) is True
    rotated = await client.rotate_key(KEY["key_hash"], new_hash)
    assert rotated == {
        "rotated": True, "already_rotated": False, "key_id": "k1"
    }
    assert await client.set_key_enabled(KEY["key_hash"], False) is True
    assert await client.delete_key(KEY["key_hash"]) is True
    await client.aclose()

    assert [(request.method, request.url.path) for request in calls] == [
        ("GET", "/keys"),
        ("POST", "/keys"),
        ("POST", "/keys/rotate"),
        ("POST", "/keys/set-enabled"),
        ("DELETE", f"/keys/{KEY['key_hash']}"),
    ]
    assert _json(calls[1]) == {
        "key_hash": KEY["key_hash"],
        "key_id": "k1",
        "rpm": 60,
        "tpm": None,
        "models": ["deepseek-v4.1-flash-free"],
    }
    assert _json(calls[2]) == {
        "key_hash": KEY["key_hash"], "new_key_hash": new_hash
    }
    assert _json(calls[3]) == {
        "key_hash": KEY["key_hash"], "enabled": False
    }


# --- affinity / personas ----------------------------------------------------


@pytest.mark.asyncio
async def test_affinity_and_persona_methods():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/affinity" and request.method == "GET":
            return _ok(request, {"bindings": [BINDING]})
        if path == "/affinity" and request.method == "POST":
            return _ok(request, {"written": 2, "unbound": 1})
        if path == "/personas" and request.method == "GET":
            return _ok(request, {"personas": [PERSONA]})
        if path == "/personas" and request.method == "POST":
            return _ok(request, {"written": True})
        if request.url.raw_path == b"/personas/proxy-a.invalid%3A8080":
            return _ok(request, PERSONA)
        return httpx.Response(404, request=request)

    client, calls = _client(handler)
    assert await client.load_affinity() == [BINDING]
    assert await client.upsert_affinity([
        {
            "session_key": "sid:ses_abc",
            "proxy": PROXY["proxy"],
            "created_at": 10,
            "last_used_at": 20,
        },
        # The legacy tuple shape stays accepted by the adapter.
        ("sid:ses_def", None, 11.0, 21.0),
    ]) == {"written": 2, "unbound": 1}

    assert await client.load_personas() == [PERSONA]
    assert await client.upsert_persona(SimpleNamespace(**PERSONA)) is True
    assert await client.get_persona("proxy-a.invalid:8080") == PERSONA
    assert await client.get_persona("[::1]:8080") is None
    await client.aclose()

    assert [(request.method, request.url.path) for request in calls] == [
        ("GET", "/affinity"),
        ("POST", "/affinity"),
        ("GET", "/personas"),
        ("POST", "/personas"),
        ("GET", "/personas/proxy-a.invalid:8080"),
        ("GET", "/personas/[::1]:8080"),
    ]
    assert _json(calls[1]) == {
        "bindings": [
            {
                "session_key": "sid:ses_abc",
                "proxy": PROXY["proxy"],
                "created_at": 10.0,
                "last_used_at": 20.0,
            },
            {
                "session_key": "sid:ses_def",
                "proxy": None,
                "created_at": 11.0,
                "last_used_at": 21.0,
            },
        ]
    }
    assert _json(calls[3]) == PERSONA

    # The persona quota key is URL-encoded as one path segment.
    assert calls[4].url.raw_path == b"/personas/proxy-a.invalid%3A8080"
    assert calls[5].url.raw_path == b"/personas/%5B%3A%3A1%5D%3A8080"


@pytest.mark.asyncio
async def test_persona_path_encodes_hostile_quota_keys():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404, request=request)

    client = StorageClient(BASE, transport=httpx.MockTransport(handler))
    assert await client.get_persona("host name:8080/../x") is None
    await client.aclose()

    assert len(calls) == 1
    assert calls[0].url.raw_path == (
        b"/personas/host%20name%3A8080%2F..%2Fx"
    )


# --- request log / stats ----------------------------------------------------


@pytest.mark.asyncio
async def test_request_log_and_stats_payloads():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/request-log":
            return _ok(request, {"logged": True})
        if path == "/request-log/recent":
            return _ok(request, {"entries": [ENTRY], "limit": 5})
        if path == "/stats/summary":
            return _ok(request, {
                "k1": {"requests": 1, "errors": 0, "tokens": 15,
                       "avg_latency_ms": 123.4},
                "total": {"requests": 1, "errors": 0, "tokens": 15,
                          "avg_latency_ms": 123.4},
            })
        if path == "/stats/by-account":
            return _ok(request, {
                "1.2.3.4:8080": {"requests": 1, "errors": 0, "tokens": 15},
            })
        if path == "/stats/daily":
            return _ok(request, [{
                "day": "2026-05-20", "account": "1.2.3.4:8080",
                "requests": 1, "errors": 0, "tokens": 15,
            }])
        return httpx.Response(404, request=request)

    client, calls = _client(handler)
    entry = LogEntry(**ENTRY)
    await client.log_request(entry)
    await client.log_request({**ENTRY, "ignored": "must not be sent"})
    assert await client.recent_requests(5) == [ENTRY]

    summary = await client.stats_summary(since=100.5)
    assert summary["total"]["tokens"] == 15
    assert await client.stats_by_account() == {
        "1.2.3.4:8080": {"requests": 1, "errors": 0, "tokens": 15}
    }
    assert await client.stats_daily() == [{
        "day": "2026-05-20", "account": "1.2.3.4:8080",
        "requests": 1, "errors": 0, "tokens": 15,
    }]
    await client.aclose()

    assert [(request.method, request.url.path) for request in calls] == [
        ("POST", "/request-log"),
        ("POST", "/request-log"),
        ("GET", "/request-log/recent"),
        ("GET", "/stats/summary"),
        ("GET", "/stats/by-account"),
        ("GET", "/stats/daily"),
    ]
    sent = _json(calls[0])
    assert sent == ENTRY
    assert [request.method for request in calls].count("POST") == 2
    # Unknown mapping keys are dropped and defaults are explicit.
    assert _json(calls[1]) == ENTRY
    assert json.loads(calls[1].content) == ENTRY
    assert "ignored" not in _json(calls[1])
    assert calls[2].url.params["limit"] == "5"
    assert calls[3].url.params["since"] == "100.5"
    assert "since" not in calls[4].url.params
    assert calls[5].url.params["since"] == "0.0"


# --- error mapping ----------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        # The private service has no auth: an unexpected 401/403 means the call
        # was misrouted or the service is unusable, so it degrades like 5xx.
        (401, StorageUnavailable),
        (403, StorageUnavailable),
        (404, StorageNotFound),
        (409, StorageConflict),
        (422, StorageAPIError),
        (429, StorageAPIError),
        (500, StorageUnavailable),
        (503, StorageUnavailable),
    ],
)
async def test_status_to_exception_mapping_is_generic(status, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"message": SECRET_BODY, "token": SECRET}},
            request=request,
        )

    client = StorageClient(BASE, transport=httpx.MockTransport(handler))
    with pytest.raises(expected) as caught:
        await client.list_keys()
    await client.aclose()

    message = str(caught.value)
    assert SECRET_BODY not in message
    assert SECRET not in message
    assert caught.value.status_code == status
    if expected is StorageAPIError:
        assert str(status) in message


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_and_timeout_map_to_unavailable(exc):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc(f"{SECRET_BODY} {SECRET}")

    client = StorageClient(BASE, transport=httpx.MockTransport(handler))
    with pytest.raises(StorageUnavailable) as caught:
        await client.check()
    assert await client.ping() is False
    await client.aclose()

    assert SECRET_BODY not in str(caught.value)
    assert SECRET not in str(caught.value)
    assert exc.__name__ in str(caught.value)


@pytest.mark.asyncio
async def test_degraded_health_and_invalid_json_are_reported_safely():
    def degraded(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": SECRET_BODY}, request=request)

    client = StorageClient(BASE, transport=httpx.MockTransport(degraded))
    with pytest.raises(StorageUnavailable) as caught:
        await client.check()
    await client.aclose()
    assert SECRET_BODY not in str(caught.value)

    def invalid_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SECRET_BODY, request=request)

    client = StorageClient(
        BASE, transport=httpx.MockTransport(invalid_json)
    )
    with pytest.raises(StorageAPIError) as caught:
        await client.list_keys()
    await client.aclose()
    assert caught.value.status_code == 200
    assert SECRET_BODY not in str(caught.value)


# --- lifecycle --------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_context_manager_and_injected_client_ownership():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok(request, {"keys": []})

    client, _ = _client(handler)
    async with client as entered:
        assert entered is client
        assert await entered.list_keys() == []
        assert client.closed is False
    assert client.closed is True
    await client.aclose()  # idempotent

    injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    wrapper = StorageClient(BASE, client=injected)
    assert await wrapper.list_keys() == []
    assert injected.is_closed is False
    await wrapper.aclose()
    assert injected.is_closed is True


def test_client_rejects_both_client_and_transport():
    injected = httpx.AsyncClient()
    with pytest.raises(ValueError):
        StorageClient(
            BASE, client=injected,
            transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        )

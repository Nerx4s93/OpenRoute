"""Hermetic tests for the curl_cffi upstream facade (gateway/curlclient.py).

No network, no real curl session: ``curl_requests.AsyncSession`` is replaced
with a stub. What is verified HERE is the facade contract:

- the byte-matched opencode fingerprint (ja3 + sigalgs + forced HTTP/1.1 +
  accept-encoding + no cookie jar) is passed to EVERY request;
- header order is normalised to the real client's wire order and
  curl-managed transport headers are never duplicated in the custom block;
- ``aread()`` is idempotent and merges partially streamed bytes;
- curl errors surface as ``httpx.TransportError`` (caught by the gateway's
  ``except httpx.HTTPError`` handlers);
- ``aclose()`` aborts an in-flight transfer via ``quit_now`` instead of
  waiting for the server to finish generating.
"""

import asyncio

import httpx
import pytest
from curl_cffi.const import CurlHttpVersion
from curl_cffi.requests.exceptions import RequestException

from gateway import curlclient, opencode
from gateway.curlclient import Response, UpstreamClient, _ordered, _timeout_tuple

CHAT_PATH = "/inference/openai/v1/chat/completions"


class FakeRaw:
    """Just enough of curl_cffi's streaming Response for the facade."""

    def __init__(self, chunks=(b"a", b"b", b"c"), status_code=200):
        self.status_code = status_code
        self.headers = {"content-type": "application/json", "retry-after": "7"}
        self._left = list(chunks)
        self.acontent_calls = 0
        self.quit_now = None
        self.astream_task = None

    async def acontent(self):
        self.acontent_calls += 1
        assert self.acontent_calls == 1, "acontent() must be called at most once"
        out = b"".join(self._left)
        self._left.clear()
        return out

    async def aiter_content(self):
        while self._left:
            yield self._left.pop(0)


class FakeSession:
    def __init__(self, **kw):
        self.init_kw = kw
        self.calls = []
        self.closed = False

    async def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return FakeRaw()

    async def close(self):
        self.closed = True


@pytest.fixture()
def fake_sessions(monkeypatch):
    made = []

    def factory(**kw):
        s = FakeSession(**kw)
        made.append(s)
        return s

    monkeypatch.setattr(curlclient.curl_requests, "AsyncSession", factory)
    return made


def _chat_request(client, **kw):
    return client.build_request(
        "POST", CHAT_PATH,
        headers=kw.get("headers", {"Authorization": "Bearer public"}),
        content=kw.get("content", b"{}"),
    )


# --- header normalisation ---------------------------------------------------


def test_ordered_matches_capture_and_drops_curl_managed():
    scrambled = {
        "x-session-id": "S",
        "Authorization": "Bearer public",
        "accept": "*/*",  # curl adds its own: must never be in the custom block
        "x-opencode-project": "P",
        "content-type": "application/json",
        "Accept-Encoding": "gzip",  # ditto
        "b3": "b",
        "Host": "opencode.ai",  # ditto
        "x-opencode-client": "cli",
        "User-Agent": opencode.OPENCODE_UA,
        "traceparent": "t",
        "Content-Length": "2",  # ditto
        "x-opencode-session": "S",
        "connection": "keep-alive",  # ditto
        "x-session-affinity": "S",
        "x-opencode-session-id": "S",
        "x-unknown-extra": "1",  # unknown: kept, after the known block
    }
    out = _ordered(scrambled)
    # order is matched case-insensitively; the original casing is preserved
    # (the gateway already emits the real client's wire casing)
    assert [k.lower() for k in out] == [
        *opencode.OPENCODE_HEADER_ORDER, "x-unknown-extra"]
    assert out["Authorization"] == "Bearer public"
    lowers = [k.lower() for k in out]
    for managed in ("accept", "accept-encoding", "host", "content-length",
                    "connection"):
        assert managed not in lowers


def test_ordered_empty_and_none():
    assert _ordered(None) == {}
    assert _ordered({}) == {}


def test_build_request_joins_base_url_and_keeps_order(fake_sessions):
    c = UpstreamClient(base_url="https://opencode.ai/")
    req = c.build_request(
        "POST", f"{CHAT_PATH}", params=[],
        headers={"x-session-id": "S", "Authorization": "Bearer public"},
        content=b"{}")
    assert req.url == f"https://opencode.ai{CHAT_PATH}"
    assert list(req.headers) == ["Authorization", "x-session-id"]
    # absolute URLs pass through untouched (health probe)
    probe = c.build_request("GET", "https://opencode.ai/inference/v1/models")
    assert probe.url == "https://opencode.ai/inference/v1/models"


# --- fingerprint application -------------------------------------------------


def test_send_applies_bun_fingerprint_on_every_request(fake_sessions):
    c = UpstreamClient(base_url="https://opencode.ai", proxy="http://p:1")
    req = _chat_request(c, headers={
        "Authorization": "Bearer public", "x-session-id": "S"})
    resp = asyncio.run(c.send(req))
    assert resp.status_code == 200
    s = fake_sessions[0]
    assert s.init_kw["proxy"] == "http://p:1"
    method, url, kw = s.calls[0]
    assert (method, url) == ("POST", f"https://opencode.ai{CHAT_PATH}")
    assert kw["ja3"] == opencode.OPENCODE_TLS_JA3
    assert kw["extra_fp"]["tls_signature_algorithms"] == list(
        opencode.OPENCODE_TLS_SIGALGS)
    assert kw["http_version"] == CurlHttpVersion.V1_1
    assert kw["accept_encoding"] == opencode.OPENCODE_ACCEPT_ENCODING
    assert kw["discard_cookies"] is True  # Bun's fetch keeps no cookie jar
    assert kw["stream"] is True  # buffering is the facade's job
    assert kw["data"] == b"{}"
    assert kw["params"] is None  # empty chat params list
    assert list(kw["headers"]) == ["Authorization", "x-session-id"]
    # the fingerprint comes from the constants, not from session defaults:
    assert "ja3" not in s.init_kw


def test_send_wraps_curl_errors_as_httpx_transport_error(fake_sessions):
    c = UpstreamClient(base_url="https://opencode.ai")
    req = _chat_request(c)

    async def boom(method, url, **kw):
        raise RequestException("curl: (7) couldn't connect")

    fake_sessions[0].request = boom  # instance attribute shadows the method
    with pytest.raises(httpx.TransportError) as ei:
        asyncio.run(c.send(req))
    # the gateway's retry/parking logic catches plain httpx.HTTPError
    assert isinstance(ei.value, httpx.HTTPError)
    assert "curl: (7)" in str(ei.value)


# --- timeout mapping ---------------------------------------------------------


def test_timeout_tuple_mapping():
    assert _timeout_tuple(httpx.Timeout(300.0, connect=10.0)) == (10.0, 300.0)
    assert _timeout_tuple(httpx.Timeout(5.0)) == (5.0, 5.0)
    assert _timeout_tuple(12.5) == (5.0, 12.5)
    assert _timeout_tuple(None) == (5.0, 300.0)


def test_client_passes_timeout_tuple_to_session(fake_sessions):
    UpstreamClient(base_url="https://opencode.ai",
                   timeout=httpx.Timeout(300.0, connect=10.0))
    assert fake_sessions[0].init_kw["timeout"] == (10.0, 300.0)


# --- Response facade semantics ----------------------------------------------


def test_response_aread_is_idempotent():
    raw = FakeRaw()
    r = Response(raw)

    async def go():
        return await r.aread(), await r.aread()

    first, second = asyncio.run(go())
    assert first == second == b"abc"
    assert raw.acontent_calls == 1  # second read is served from the buffer


def test_response_stream_then_read_merges_seen_bytes():
    raw = FakeRaw(chunks=[b"x", b"y", b"z"])
    r = Response(raw)

    async def go():
        got = []
        async for chunk in r.aiter_bytes():
            got.append(chunk)
            break  # client disconnect mid-stream
        return got, await r.aread()

    got, whole = asyncio.run(go())
    assert got == [b"x"]
    assert whole == b"xyz"  # already-seen bytes + the rest of the queue


def test_response_read_then_stream_replays_buffer():
    raw = FakeRaw(chunks=[b"q", b"r"])
    r = Response(raw)

    async def go():
        body = await r.aread()
        replayed = [chunk async for chunk in r.aiter_bytes()]
        return body, replayed

    body, replayed = asyncio.run(go())
    assert body == b"qr"
    assert replayed == [b"qr"]
    assert raw.acontent_calls == 1


def test_response_stream_errors_become_httpx_errors():
    raw = FakeRaw()

    async def failing():
        yield b"partial"
        raise RequestException("curl: (23) failed writing body")

    raw.aiter_content = failing
    r = Response(raw)

    async def go():
        try:
            [chunk async for chunk in r.aiter_bytes()]
        except httpx.TransportError as exc:
            return exc
        raise AssertionError("no httpx error raised")

    exc = asyncio.run(go())
    assert isinstance(exc, httpx.HTTPError)


def test_response_headers_are_the_raw_headers():
    raw = FakeRaw()
    r = Response(raw)
    assert r.headers.get("retry-after") == "7"
    assert r.status_code == 200


def test_aclose_aborts_transfer_via_quit_now():
    async def go():
        raw = FakeRaw()
        released = asyncio.Event()

        async def pump():  # stands in for curl_cffi's stream pump task
            await raw.quit_now.wait()
            released.set()

        raw.quit_now = asyncio.Event()
        raw.astream_task = asyncio.create_task(pump())
        r = Response(raw)
        # abort must not wait for the (never-ending) transfer
        await asyncio.wait_for(r.aclose(), timeout=1.0)
        await asyncio.wait_for(r.aclose(), timeout=1.0)  # idempotent
        assert released.is_set()
        raw.astream_task.cancel()

    asyncio.run(asyncio.wait_for(go(), timeout=3.0))


# --- httpx niceties used by the health probe ---------------------------------


def test_get_buffers_body_like_httpx(fake_sessions):
    async def go():
        async with UpstreamClient(base_url="https://opencode.ai") as c:
            return await c.get("https://opencode.ai/inference/v1/models")

    r = asyncio.run(go())
    s = fake_sessions[0]
    assert s.closed is True  # __aexit__ closed the session
    method, url, kw = s.calls[0]
    assert (method, url) == ("GET", "https://opencode.ai/inference/v1/models")
    assert r.status_code == 200
    body = asyncio.run(r.aread())  # buffered by get(): no second acontent
    assert body == b"abc"

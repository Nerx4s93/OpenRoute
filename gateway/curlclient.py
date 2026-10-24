"""Upstream transport: curl_cffi with the REAL opencode TLS fingerprint.

Why this module exists (measured 2026-10-24, see ``opencode.py`` docstring):
the upstream free tier fingerprints the TLS ClientHello. httpx (OpenSSL),
curl (Schannel) and Chrome impersonation all get an instant
``429 FreeUsageLimitError`` — regardless of the request bytes — and poison
the exit IP for every other client, the genuine CLI included. curl_cffi is
built on the same BoringSSL the real client (Bun) uses, and with
:data:`opencode.OPENCODE_TLS_JA3` + :data:`opencode.OPENCODE_TLS_SIGALGS`
produces a ClientHello that is field-identical to the genuine one
(ciphers, extension order/set, X25519MLKEM768+X25519 key shares, ALPN
``http/1.1`` only, full 11-entry sigalgs list). Verified live through a
never-used proxy: 200 SSE with the ordinary gateway body.

This is a drop-in for the subset of the ``httpx.AsyncClient`` API the
gateway's request path uses (``build_request``/``send`` +
``Response.status_code/headers/aread/aclose/aiter_bytes``), so
``gateway.main`` and the test harness' scripted transports are unchanged.
Transport-level failures are re-raised as ``httpx.TransportError`` so the
existing ``except httpx.HTTPError`` retry/parking logic keeps working.

curl_cffi streaming specifics the facade papers over (0.15.0):

- ``Response.acontent()`` is single-shot: re-iterating a finished stream
  hangs forever on the (empty) queue. The facade buffers, making
  ``aread()`` idempotent like httpx.
- The async ``aclose()`` only awaits the pump task; it never aborts the
  in-flight transfer, so abandoning an SSE stream mid-way would block
  until the server finishes generating. The facade sets ``quit_now``
  first (curl aborts on the next write callback) and only then waits.
- Errors surface from the stream queue as ``RequestException`` (=
  ``RequestsError``); the facade converts them to ``httpx.TransportError``
  so ``except httpx.HTTPError`` handlers keep catching them.
"""

from __future__ import annotations

import httpx
from curl_cffi import requests as curl_requests
from curl_cffi.const import CurlHttpVersion
from curl_cffi.requests.exceptions import RequestException

from . import opencode

#: Headers curl manages itself (Accept, Accept-Encoding, Host,
#: Content-Length). Passing them in the custom block would duplicate them
#: AND break the genuine client's layout (custom block first, transport
#: headers in the tail).
_CURL_MANAGED_HEADERS = frozenset({
    "accept", "accept-encoding", "host", "content-length", "connection",
})


class _PreparedRequest:
    """Mirrors what main.py needs from httpx.Request (nothing but the send)."""

    __slots__ = ("method", "url", "params", "headers", "content")

    def __init__(self, method: str, url: str, params, headers, content):
        self.method = method
        self.url = url
        self.params = params
        self.headers = headers
        self.content = content


def _to_httpx_error(exc: RequestException) -> httpx.TransportError:
    return httpx.TransportError(f"{type(exc).__name__}: {exc}")


class Response:
    """httpx.Response facade over a curl_cffi streaming Response."""

    def __init__(self, raw):
        self._raw = raw
        self._content: bytes | None = None  # set once fully read
        self._seen = bytearray()  # chunks already yielded by aiter_bytes

    @property
    def status_code(self) -> int:
        return self._raw.status_code

    @property
    def headers(self):
        return self._raw.headers

    async def aread(self) -> bytes:
        """Whole (decompressed) body; idempotent (httpx semantics)."""
        if self._content is None:
            # acontent() consumes only what is LEFT in the stream queue,
            # so bytes already streamed via aiter_bytes() are merged back in.
            rest = await self._raw.acontent()
            self._content = bytes(self._seen) + rest
            self._seen.clear()
        return self._content

    async def aiter_bytes(self):
        if self._content is not None:
            yield self._content
            return
        try:
            async for chunk in self._raw.aiter_content():
                self._seen.extend(chunk)
                yield chunk
        except RequestException as exc:
            raise _to_httpx_error(exc) from exc
        else:
            self._content = bytes(self._seen)
            self._seen.clear()

    async def aclose(self) -> None:
        """Abort the transfer if still running, then release resources.

        Idempotent and safe after a full read (the pump task is already
        finished in that case; awaiting it again returns immediately).
        """
        raw = self._raw
        quit_now = getattr(raw, "quit_now", None)
        if quit_now is not None:
            quit_now.set()  # curl aborts on the next write callback
        task = getattr(raw, "astream_task", None)
        if task is not None:
            try:
                await task
            except Exception:  # the pump always finishes with a queue signal
                pass


def _ordered(headers) -> dict:
    """Re-order the upstream headers into the real client's wire order.

    ``_upstream_headers`` builds a plain dict whose insertion order differs
    from the genuine client's. curl_cffi emits custom headers in insertion
    order, so the order is normalised here; curl-managed transport headers
    are dropped (curl adds its own at the tail, exactly like the real
    client's HTTP stack does). Unknown headers (none today) keep their
    relative order after the known block.
    """
    lower = {}
    for name, value in (headers or {}).items():
        lower.setdefault(name.lower(), (name, value))
    known, extra = [], []
    for name in opencode.OPENCODE_HEADER_ORDER:
        pair = lower.pop(name, None)
        if pair is not None:
            known.append(pair)
    for pair in lower.values():
        if pair[0].lower() not in _CURL_MANAGED_HEADERS:
            extra.append(pair)
    return dict(known + extra)


def _timeout_tuple(timeout) -> tuple[float, float]:
    """httpx.Timeout -> curl_cffi ``(connect, read)``.

    curl_cffi maps the tuple onto CONNECTTIMEOUT + (for streaming) a
    LOW_SPEED abort ("no data for N s"), which matches httpx's per-read
    semantics for SSE; a scalar becomes ``(connect, total)`` the same way.
    """
    if isinstance(timeout, httpx.Timeout):
        connect = timeout.connect or 5.0
        read = max(timeout.read or 0.0, timeout.write or 0.0) or 30.0
        return float(connect), float(read)
    if timeout is None:
        return 5.0, 300.0
    return 5.0, float(timeout)


class UpstreamClient:
    """Per-proxy upstream client with the byte-matched TLS fingerprint."""

    def __init__(self, *, base_url: str = "", proxy: str | None = None,
                 timeout=httpx.Timeout(300.0, connect=10.0),
                 verify: bool | str = True):
        self._base = (base_url or "").rstrip("/")
        self._timeout = _timeout_tuple(timeout)
        self._closed = False
        # ``verify`` may be a path to a CA bundle, exactly like in httpx
        # (curl_cffi maps it to CURLOPT_CAINFO). The container uses the
        # default certifi bundle; ASCII paths only.
        self._session = curl_requests.AsyncSession(
            proxy=proxy, verify=verify, timeout=self._timeout)

    def build_request(self, method: str, url: str, *, params=None,
                      headers=None, content=None) -> _PreparedRequest:
        if url.startswith("http://") or url.startswith("https://"):
            full = url
        else:
            full = f"{self._base}/{url.lstrip('/')}"
        return _PreparedRequest(method, full, params, _ordered(headers), content)

    async def send(self, request: _PreparedRequest, stream: bool = True) -> Response:
        # curl_cffi always runs the request in streaming mode here; buffering
        # is done by Response.aread() consuming the body. The fingerprint is
        # applied per request: any of it drifting in a curl_cffi upgrade would
        # silently change the wire, so it is set from the opencode constants
        # every time, never cached.
        try:
            raw = await self._session.request(
                request.method, request.url,
                params=request.params or None,
                headers=request.headers,
                data=request.content,
                stream=True,
                ja3=opencode.OPENCODE_TLS_JA3,
                extra_fp={"tls_signature_algorithms":
                          list(opencode.OPENCODE_TLS_SIGALGS)},
                http_version=CurlHttpVersion.V1_1,
                accept_encoding=opencode.OPENCODE_ACCEPT_ENCODING,
                discard_cookies=True,  # Bun's fetch keeps no cookie jar
                timeout=self._timeout,
            )
        except RequestException as exc:
            raise _to_httpx_error(exc) from exc
        return Response(raw)

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._session.close()

    @property
    def is_closed(self) -> bool:
        """httpx.AsyncClient parity (used by the proxy manager tests)."""
        return self._closed

    # httpx.AsyncClient niceties used by the health probe ---------------

    async def get(self, url: str, *, params=None, headers=None) -> Response:
        """Buffered GET (httpx semantics: the body is fully read)."""
        resp = await self.send(self.build_request(
            "GET", url, params=params, headers=headers))
        await resp.aread()
        return resp

    async def __aenter__(self) -> "UpstreamClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

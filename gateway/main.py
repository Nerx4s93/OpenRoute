"""OpenRoute gateway (data plane) — OpenAI-compatible passthrough.

Clients authenticate with an OpenRoute key (``keys.KeyStore``); the gateway
round-robins the request across a pool of HTTP(S) proxies
(``proxies.ProxyManager``) and forwards to opencode.ai's **free-tier**
inference. The differences from ``NVRoute/app/main.py``:

- the upstream is opencode.ai: the gate headers + gated system message come
  from :mod:`gateway.opencode`; the client's ``Authorization`` is NOT forwarded —
  the gate always carries ``Bearer public`` + a session id (the caller's own
  affinity when it sends one, otherwise one id per conversation),
- the pool is proxies (no tokens): ``ProxyManager`` instead of
  ``AccountManager``; ``LogEntry.account`` carries the masked proxy,
- the gate **forces upstream streaming**: upstream always answers SSE. If the
  caller asked for ``stream: true`` the SSE is passed through verbatim; if the
  caller asked for a non-stream answer, the upstream SSE is fully buffered and
  converted into a standard OpenAI non-stream ``chat.completion`` JSON object
  (aggregated message + ``usage``). This keeps ordinary OpenAI clients working
  against a streaming-only upstream.

Request flow for ``ANY /v1/{rest:path}``:

1. auth via ``KeyStore`` (``require_auth`` / ``require_admin``),
2. anti-DoS body limit (``MAX_BODY_BYTES``, 413 without reading when possible),
3. model extraction + allow-list (403), then RPM/TPM gates (429),
4. session id resolution ONCE for the whole request: caller affinity ->
   conversation fingerprint -> one-shot (:func:`_resolve_session_id`),
5. retry loop over the session lease: ``affinity.next_proxy(lease, exclude=tried)``
   when sticky sessions are enabled, else ``ProxyManager.next_proxy(exclude=tried)``:
   * build the upstream request with gate headers/body (:mod:`gateway.opencode`),
   * on ``httpx.HTTPError`` -> ``mark_failure`` + backoff + next proxy,
   * on ``429 FreeUsageLimitError`` -> park that proxy's quota bucket until
     the next 00:00 UTC, evacuate its sessions, retry on another proxy,
   * on any other 429 -> pass the upstream body + ``Retry-After`` through to
     the client (no cooldown, parking or rotation),
   * on 401 / 403 with a body-level ``FreeTierError`` verdict (the upstream
     gate: "not from OpenCode") -> pass the verdict through to the client:
     every proxy answers the same, so retrying would only burn the pool,
   * on ``403 abusive traffic`` -> ban that proxy's quota bucket, evacuate
     its sessions, retry on another proxy,
   * on 5xx / other 401 / 403 -> ``mark_failure`` + next proxy,
   * other statuses (2xx/4xx) are the client's answer,
6. stream via ``StreamingResponse`` (SSE, no buffering) or convert, or return
   the body for non-chat endpoints.

Retry budget: ``RETRY_BUDGET_SECONDS`` -> 504 when exhausted. Each proxy is
tried at most once per request (``tried`` set). No ready proxy -> 429 with
``Retry-After`` when the pool is merely saturated, else 502/503.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import opencode, sessions
from .affinity import AffinityLease, AffinityManager, SessionIdentity
from .affinitydb import AffinityDB
from .config import PROVIDER_NAME, get_settings
from .keydb import KeyDB
from .keys import AuthError, KeyInfo, KeyStore, RateLimitError, hash_key
from .proxies import NoReadyProxyError, Proxy, ProxyManager, mask_proxy
from .proxydb import ProxyDB
from .reqlog import LogEntry, RequestLog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("openroute")

settings = get_settings()
manager = ProxyManager(settings)
keystore = KeyStore()
reqlog = RequestLog(settings.postgres_dsn)
# Conversation fingerprint -> upstream session id (a caller's affinity wins).
session_registry = sessions.SessionRegistry()
# PostgreSQL stores (proxies/keys tables in the same DB) are created in the
# lifespan — tests patch settings per-gateway, so the DSN is taken at startup,
# not at module import.
proxydb: ProxyDB | None = None
keydb: KeyDB | None = None
affinitydb: AffinityDB | None = None
affinity: AffinityManager | None = None

# Strong refs to fire-and-forget tasks (log writes): create_task returns a
# weakref-compatible Future — without a set the task can be GC'd in flight.
_bg_tasks: set[asyncio.Task] = set()

# Headers never forwarded to the upstream. x-forwarded-*/x-real-ip (client
# topology), cookie and x-api-key must never reach the upstream.
HOP_HEADERS = {
    "host", "authorization", "content-length", "connection", "keep-alive",
    "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
    "proxy-connection", "accept-encoding",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "x-forwarded-port", "x-real-ip", "cookie", "x-api-key",
}
# content-encoding is dropped because the stream is read via aiter_bytes():
# httpx decompresses the body itself and the client gets decoded bytes.
RESP_DROP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection"}

_INTERNAL_PREFIX = "/internal"

# --- model catalogue cache (for GET /v1/models) ---
_MODELS_TTL = 300.0
_models_cache: dict | None = None
_models_cache_ts = 0.0


# --- lifecycle -------------------------------------------------------------


async def refresh_loop() -> None:
    while True:
        try:
            ok = await manager.refresh_proxies()
        except Exception:
            # one unexpected crash must not silently kill refresh forever:
            # log and retry on the fast interval.
            logger.exception("refresh_proxies crashed; will retry proxy refresh soon")
            ok = False
        # On fetch error (e.g. the neighbouring container is not up yet) retry
        # quickly instead of waiting the full interval.
        await asyncio.sleep(settings.proxy_refresh_seconds if ok else settings.proxy_retry_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global proxydb, keydb, affinitydb, affinity
    # The proxy pool and client keys live in PostgreSQL and are populated only
    # via the internal API (admin UI). env/files no longer participate.
    proxydb = ProxyDB(settings.postgres_dsn)
    manager.bind_db(proxydb)
    rows = proxydb.all()
    manager.apply_proxies(rows)
    try:
        parking = proxydb.parking_all()
        manager.load_parking(parking)
    except Exception:
        logger.exception("Failed to load proxy parking; starting unparked")
    logger.info("Loaded %d proxies from PostgreSQL (%d enabled)",
                len(rows), sum(1 for _, en in rows if en))
    if not rows:
        logger.warning("No proxies configured: add proxies via the admin UI "
                       "(or PROXIES_URL will populate them on refresh)")
    keydb = KeyDB(settings.postgres_dsn)
    keystore.load(keydb.all())
    if not keystore.key_ids():
        logger.warning("No client keys configured: all /v1 requests get 401 "
                       "until keys are added via the admin UI")
    # Sticky sessions: load persisted bindings (S survives a restart; R/T are
    # in-memory and start empty). The manager is created even when disabled so
    # /health and /admin/status keep a stable shape.
    if affinitydb is None:
        affinitydb = AffinityDB(settings.postgres_dsn)
    if affinity is None:
        affinity = AffinityManager(settings, manager, db=affinitydb)
    try:
        await affinity.load()
    except Exception:
        logger.exception("Failed to load session affinity; continuing without it")
    logger.info("Session affinity enabled=%s (window=%.0fs)",
                settings.affinity_enabled, settings.affinity_window_seconds)
    task = asyncio.create_task(refresh_loop())
    logger.info("%s gateway started: upstream=%s, proxies from %s",
                PROVIDER_NAME, settings.opencode_base_url, settings.proxies_url)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    if affinity is not None:
        await affinity.aclose()
        affinity = None
        affinitydb = None  # closed by AffinityManager.aclose()
    elif affinitydb is not None:
        affinitydb.close()
        affinitydb = None
    if keydb is not None:
        keydb.close()
        keydb = None
    if proxydb is not None:
        proxydb.close()
        proxydb = None
    reqlog.close()
    await manager.aclose()


# --- error envelope --------------------------------------------------------


def _openai_error(status: int, detail: str, err_type: str,
                  headers: dict | None = None) -> JSONResponse:
    """OpenAI-shaped errors (``{"error": {...}}``) so clients understand them."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": detail, "type": err_type, "code": status}},
        headers=headers,
    )


async def openai_error_handler(request: Request, exc: StarletteHTTPException):
    # 3xx with Location (e.g. "no session -> /login") is a redirect, not an
    # OpenAI-format error.
    hdrs = exc.headers or {}
    loc = hdrs.get("Location") or hdrs.get("location")
    if loc and exc.status_code in (301, 302, 303, 307, 308):
        return RedirectResponse(loc, status_code=exc.status_code)
    return _openai_error(exc.status_code, exc.detail, "invalid_request_error",
                         headers=getattr(exc, "headers", None))


async def auth_error_handler(request: Request, exc: AuthError):
    return _openai_error(exc.status_code, exc.detail, "invalid_request_error")


async def rate_limit_error_handler(request: Request, exc: RateLimitError):
    headers = None
    if exc.retry_after is not None:
        # Retry-After to the client — ceil seconds (min 1), per RFC.
        headers = {"Retry-After": str(max(1, math.ceil(exc.retry_after)))}
    return _openai_error(429, exc.detail, "rate_limit_error", headers=headers)


# --- auth: client keys (keys.py) ------------------------------------------


async def require_auth(request: Request) -> KeyInfo:
    """Bearer key from ``KeyStore``: constant-time compare, revocation.

    RPM/TPM gates are DELIBERATELY not here: the RPM limit is checked in
    ``proxy_v1`` AFTER the model allow-list (a 403 rejection must not burn the
    client's quota), and admin endpoints must not spend client limits at all.
    """
    return keystore.authenticate(request.headers.get("authorization", ""))


async def require_admin(key: KeyInfo = Depends(require_auth)) -> KeyInfo:
    """/admin/* additionally requires ``admin: true`` on the key."""
    if not key.is_admin:
        raise AuthError(403, "Admin access required")
    return key


# --- passthrough helpers ---------------------------------------------------


def _wants_stream(rest: str, body: bytes) -> bool:
    """Streaming only for completions with a truthy ``"stream"`` in the body.

    The body may be ANY valid JSON (array, null, string, number) — a non-dict
    is not a stream and not a 500. A string ``"stream": "true"`` also counts.
    """
    if body and "completions" in rest:
        try:
            data = json.loads(body)
        except ValueError:
            return False
        if isinstance(data, dict):
            s = data.get("stream")
            return s is True or (isinstance(s, str) and s.lower() == "true")
    return False


def _upstream_headers(request: Request, session_id: str) -> dict:
    """Client headers minus hop headers, overlaid with the opencode gate set.

    The client's ``Authorization`` is never forwarded; the gate headers always
    win (they carry ``Authorization: Bearer public`` + ``session_id``).
    """
    gate = opencode.build_gate_headers(session_id)
    gate_names = {name.lower() for name in gate}
    # Drop the caller's copies of any gate header (case-insensitively) so the
    # gate value replaces it instead of colliding: a plain dict with both
    # ``user-agent`` and ``User-Agent`` keys makes httpx comma-join them
    # ("testclient, opencode/..."), which would fail the upstream gate.
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_HEADERS and k.lower() not in gate_names
    }
    headers.update(gate)
    return headers


def _resolve_session(request: Request, key: KeyInfo, body: bytes) -> SessionIdentity:
    """Pick the upstream session id + sticky-affinity key ONCE per request.

    1. A real OpenCode client owns its session id: reuse the affinity value it
       already sends (the same value the upstream uses for provider stickiness)
       and key the binding by it (``sid:<id>``).
    2. Other OpenAI clients carry no session at all: reuse one id per
       conversation fingerprint until the registry TTL expires (``fp:<hash>``).
    3. Nothing to anchor on: a one-shot id (no binding, no ``session_affinity``
       row).
    """
    for name in opencode.SESSION_HEADER_NAMES:
        value = request.headers.get(name)
        if opencode.is_session_id(value):
            return SessionIdentity(value, f"sid:{value}", "sid")

    fingerprint = opencode.conversation_fingerprint(
        body,
        key_id=key.key_id,
        project=request.headers.get("x-opencode-project"),
    )
    if fingerprint is not None:
        return SessionIdentity(
            session_registry.resolve(fingerprint), f"fp:{fingerprint}", "fp")

    return SessionIdentity(opencode.new_session_id(), None, "one-shot")


def _resolve_session_id(request: Request, key: KeyInfo, body: bytes) -> str:
    """Backward-compatible wrapper returning only the upstream session id."""
    return _resolve_session(request, key, body).session_id


def _resp_headers(upstream: httpx.Response) -> dict:
    return {k: v for k, v in upstream.headers.items() if k.lower() not in RESP_DROP_HEADERS}


def _is_chat(rest: str) -> bool:
    return rest.endswith("chat/completions")


def _log_request(
    *, key_id: str, method: str, path: str, model: str | None, status: int,
    started: float, stream: bool, account: str | None,
    prompt: int | None = None, completion: int | None = None, error: str | None = None,
) -> None:
    """Fire-and-forget logging: ``log()`` never raises and never breaks a request."""
    entry = LogEntry(
        ts=time.time(), key_id=key_id, method=method, path=path, model=model,
        status=status, latency_ms=round((time.monotonic() - started) * 1000.0, 1),
        stream=stream, account=account, prompt_tokens=prompt,
        completion_tokens=completion, error=error,
    )
    try:
        task = asyncio.create_task(reqlog.log(entry))
        _bg_tasks.add(task)  # strong ref: the task is not GC'd in flight
        task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:  # no running event loop — should not happen in a request path
        pass


async def _drain(upstream: httpx.Response, stream: bool) -> None:
    """Release a failed upstream response on the retry path (aread + aclose)."""
    try:
        if stream:
            await upstream.aread()
        await upstream.aclose()
    except httpx.HTTPError:
        pass


def _raise_no_ready(exc: NoReadyProxyError) -> None:
    """Map ``NoReadyProxyError.kind`` to the client contract.

    - empty pool / all disabled -> 503 (generic, no fetch diagnostics),
    - every candidate already tried -> 502,
    - cooling / RPM-capped / quota-parked -> 429 + ``Retry-After``.
    """
    kind = getattr(exc, "kind", None)
    if kind == "excluded":
        raise HTTPException(502, "All upstream attempts failed")
    if kind == "empty" or (kind is None and exc.retry_after is None):
        # Generic on purpose: the fetch status (URL/DSN/credentials) is only
        # exposed to admins via /admin/status, never to clients.
        raise HTTPException(503, "No enabled proxies")
    if exc.retry_after is None:
        raise HTTPException(503, "No enabled proxies")
    raise RateLimitError(
        "No ready upstream proxy (cooling, RPM-capped or quota/banned); "
        f"earliest slot in {math.ceil(exc.retry_after)}s",
        retry_after=exc.retry_after,
    )


async def _fetch_models_catalogue() -> dict:
    """Fetch the opencode model catalogue (no gate/proxy needed). Raises on error."""
    timeout = httpx.Timeout(settings.health_check_timeout, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(settings.opencode_models_url)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return {"object": "list", "data": data["data"]}
    if isinstance(data, list):
        return {"object": "list", "data": data}
    raise ValueError("unexpected models payload")


def _fallback_models() -> dict:
    """Graceful fallback: the known free-model ids (see gateway.opencode.FREE_MODELS)."""
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "opencode", "created": 0}
            for m in opencode.FREE_MODELS
        ],
    }


async def _models_response() -> Response:
    """GET /v1/models: the opencode free-model catalogue, cached briefly."""
    global _models_cache, _models_cache_ts
    now = time.monotonic()
    if _models_cache is not None and now - _models_cache_ts < _MODELS_TTL:
        return JSONResponse(_models_cache)
    try:
        data = await _fetch_models_catalogue()
    except Exception:
        logger.warning("models catalogue fetch failed; serving fallback list", exc_info=True)
        data = _fallback_models()
    _models_cache = data
    _models_cache_ts = now
    return JSONResponse(data)


def _aggregate_sse(content: bytes, fallback_model: str | None) -> tuple[dict, dict | None]:
    """Aggregate an upstream SSE body into ``(completion, usage)``.

    ``completion`` is a standard non-stream ``chat.completion`` object built
    from the deltas; ``usage`` is the final chunk's usage (or ``None``). Never
    raises — malformed chunks are skipped.
    """
    text_parts: list[str] = []
    role = "assistant"
    finish_reason: str | None = None
    resp_id = "chatcmpl-openroute"
    created = int(time.time())
    model = fallback_model
    usage: dict | None = None

    for raw in content.splitlines():
        try:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        except Exception:
            continue
        payload = opencode.parse_sse_line(line)
        if not payload or payload == opencode.SSE_DONE:
            continue
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("id"), str):
            resp_id = data["id"]
        if isinstance(data.get("created"), int):
            created = data["created"]
        if isinstance(data.get("model"), str):
            model = data["model"]
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            ch = choices[0] if isinstance(choices[0], dict) else {}
            delta = ch.get("delta") if isinstance(ch.get("delta"), dict) else {}
            content_part = delta.get("content")
            if isinstance(content_part, str):
                text_parts.append(content_part)
            if isinstance(delta.get("role"), str):
                role = delta["role"]
            if isinstance(ch.get("finish_reason"), str):
                finish_reason = ch["finish_reason"]

    message_content = "".join(text_parts)
    completion = {
        "id": resp_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": role, "content": message_content},
             "finish_reason": finish_reason}
        ],
    }
    return completion, usage


def _stream_gen(
    upstream: httpx.Response, proxy: Proxy, key: KeyInfo,
    method: str, rest: str, model: str | None, started: float,
    stream_usage: bool = True,
    lease: AffinityLease | None = None,
) -> AsyncIterator[bytes]:
    """Wrapper around ``aiter_bytes`` (httpx decompresses content-encoding, the
    header is honestly stripped for the client). The connection is closed on ANY
    outcome (including a client disconnect); success is recorded only after the
    full stream (opencode always streams); the log is written when the generator
    finishes. SSE usage is parsed on the fly for TPM accounting; if absent, a
    rough estimate is used (text bytes / 4) for TPM only — affinity records ONLY
    real upstream usage (or 0). All parse/count errors are swallowed and never
    break the stream.
    """

    async def gen() -> AsyncIterator[bytes]:
        error: str | None = None
        total_len = 0
        usage_total: int | None = None
        usage_prompt: int | None = None
        usage_completion: int | None = None
        completed = False
        pending = bytearray()  # an unfinished SSE line (chunk boundary mid-event)

        def real_usage_total() -> int | None:
            if usage_total is not None:
                return usage_total
            if usage_prompt is not None and usage_completion is not None:
                return usage_prompt + usage_completion
            return None

        def scan_sse_usage(chunk: bytes) -> None:
            nonlocal usage_total, usage_prompt, usage_completion
            try:
                pending.extend(chunk)
                while True:
                    nl = pending.find(b"\n")
                    if nl < 0:
                        # anti-DoS: one never-terminated line must not grow in RAM
                        if len(pending) > 1_048_576:
                            pending.clear()
                        return
                    line = bytes(pending[:nl])
                    del pending[:nl + 1]
                    payload = opencode.parse_sse_line(line)
                    parsed = opencode.usage_from_payload(payload)
                    if not parsed:
                        continue
                    if parsed.get("total_tokens") is not None:
                        usage_total = parsed["total_tokens"]
                    if parsed.get("prompt_tokens") is not None:
                        usage_prompt = parsed["prompt_tokens"]
                    if parsed.get("completion_tokens") is not None:
                        usage_completion = parsed["completion_tokens"]
            except Exception:
                logger.debug("SSE usage scan failed (TPM counting never breaks a stream)",
                             exc_info=True)

        try:
            async for chunk in upstream.aiter_bytes():
                total_len += len(chunk)
                if stream_usage:
                    scan_sse_usage(chunk)
                yield chunk
            completed = True
            manager.mark_success(proxy)  # full stream only — a disconnect is not success
        except httpx.HTTPError as exc:
            error = f"stream error: {type(exc).__name__}: {exc}"
            manager.mark_failure(proxy, error)
            raise
        finally:
            try:
                await upstream.aclose()  # real leak fix (BackgroundTask is belt-and-braces)
            except httpx.HTTPError:
                pass
            # Affinity: exactly the request event created at first send; only
            # real upstream usage (total_tokens or prompt+completion), else 0.
            if lease is not None:
                try:
                    if affinity is not None:
                        affinity.note_tokens(lease, real_usage_total() or 0)
                except Exception:
                    logger.debug("affinity stream token accounting failed", exc_info=True)
            prompt_log: int | None = None
            completion_log: int | None = None
            if error is None and upstream.status_code < 400 and stream_usage:
                try:
                    if usage_prompt is not None and usage_completion is not None:
                        total = usage_total if usage_total is not None else usage_prompt + usage_completion
                    else:
                        total = usage_total if usage_total is not None else total_len // 4
                    if total > 0:
                        keystore.count_tokens(key, total)
                    if usage_prompt is not None or usage_completion is not None:
                        prompt_log, completion_log = usage_prompt, usage_completion
                    elif total > 0:
                        prompt_log, completion_log = None, total
                except Exception:
                    logger.debug("stream TPM accounting failed", exc_info=True)
            _log_request(key_id=key.key_id, method=method, path=rest, model=model,
                         status=upstream.status_code, started=started, stream=True,
                         account=mask_proxy(proxy.url), prompt=prompt_log,
                         completion=completion_log, error=error)

    return gen()


# --- routes: data plane ----------------------------------------------------


async def proxy_v1(rest: str, request: Request, key: KeyInfo = Depends(require_auth)) -> Response:
    """ANY ``/v1/{rest:path}`` OpenAI passthrough (see module docstring)."""
    started = time.monotonic()
    model: str | None = None
    proxy_mask: str | None = None
    stream = False
    try:
        # Model catalogue is served by the gateway itself (no gate/proxy).
        if request.method == "GET" and rest == "models":
            return await _models_response()

        # Anti-DoS: the body limit is checked BEFORE buffering — a Content-Length
        # over the limit is rejected with 413 without reading the body; chunked
        # bodies are read incrementally and aborted with 413 once the
        # accumulated size exceeds the limit.
        limit = settings.max_body_bytes
        cl_raw = request.headers.get("content-length")
        if cl_raw is not None:
            try:
                cl = int(cl_raw)
            except ValueError:
                cl = None
            if cl is not None and cl > limit:
                raise HTTPException(413, f"Request body exceeds {limit} bytes")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise HTTPException(413, f"Request body exceeds {limit} bytes")

        chat = _is_chat(rest)
        upstream_body = body
        if chat:
            raw_model = opencode.extract_model(body)
            model = raw_model or settings.opencode_model
            keystore.check_model(key, raw_model)  # per-key allow-list (403)
            if model not in opencode.FREE_MODELS:
                # Gateway allow-list: only opencode free-tier models are served.
                raise AuthError(403, f"Model {model} not allowed")
            upstream_body = opencode.prepare_body(body, model)
        # RPM/TPM gates run AFTER the model allow-list: a 403 rejection must not
        # burn the client's quota. RateLimitError -> global handler -> 429.
        keystore.check_rate(key)
        keystore.check_tpm(key)
        params = list(request.query_params.multi_items())
        stream = _wants_stream(rest, body)
        identity = _resolve_session(request, key, body)
        session_id = identity.session_id
        request_wall = time.time()
        lease: AffinityLease | None = None
        if settings.affinity_enabled and affinity is not None:
            try:
                lease = affinity.begin_request(identity)
            except Exception:
                logger.exception("Affinity begin_request failed; request continues")
                lease = None

        deadline = time.monotonic() + settings.retry_budget_seconds  # overall budget
        tried: set[str] = set()  # proxy URLs already tried in this request
        attempt = 0
        proxy: Proxy
        upstream: httpx.Response
        while True:
            if attempt and time.monotonic() >= deadline:
                # retry budget exhausted: a prompt 504 instead of a worst-case hang
                raise HTTPException(504, "Upstream retry budget exhausted")
            attempt += 1
            try:
                if lease is not None and affinity is not None:
                    proxy = affinity.next_proxy(lease, exclude=tried)
                else:
                    proxy = manager.next_proxy(exclude=tried)
            except NoReadyProxyError as exc:
                _raise_no_ready(exc)
            except Exception:
                # Affinity must never break a client request: fall back to the
                # plain pool on an unexpected internal error.
                logger.exception("Affinity next_proxy failed; falling back to pool")
                try:
                    proxy = manager.next_proxy(exclude=tried)
                except NoReadyProxyError as exc:
                    _raise_no_ready(exc)
            tried.add(proxy.url)
            proxy.requests += 1
            client = manager.ensure_client(proxy)
            upstream_req = client.build_request(
                request.method, f"/{rest}", params=params,
                headers=_upstream_headers(request, session_id), content=upstream_body,
            )
            try:
                # the local RPM slot is burned only on a REAL attempt
                manager.record_request(proxy)
                if lease is not None and affinity is not None:
                    try:
                        # exactly one affinity event per client request, at the
                        # first real upstream send (tokens filled in later)
                        affinity.note_started(lease, proxy, request_wall)
                    except Exception:
                        logger.exception("Affinity note_started failed; request continues")
                upstream = await client.send(upstream_req, stream=True)
            except httpx.HTTPError as exc:  # network/proxy — transient
                reason = f"{type(exc).__name__}: {exc}"
                manager.mark_failure(proxy, reason)
                logger.warning("Proxy %s failed: %s", mask_proxy(proxy.url), reason)
                left = deadline - time.monotonic()
                if left > 0:
                    backoff = settings.retry_backoff_base_seconds * (2 ** (attempt - 1))
                    await asyncio.sleep(min(backoff, left))
                continue

            status = upstream.status_code
            if status == 429:
                # Read the body BEFORE closing: the classification is body-level.
                upstream_body_429 = await upstream.aread()
                await upstream.aclose()
                retry_after = opencode.parse_retry_after(upstream)
                if opencode.is_free_usage_limit(upstream_body_429):
                    # Daily quota: park this proxy's quota bucket until the next
                    # 00:00 UTC (+ margin), then evacuate its sessions and
                    # continue this request on another ready proxy. Buckets are
                    # per proxy URL: an IPv4 entry may exit through IPv6, so
                    # merging by entry IP would park unused proxies.
                    wait = max(opencode.seconds_to_utc_midnight(),
                               retry_after or 0.0) + settings.quota_reset_margin_seconds
                    until = time.time() + wait
                    group = manager.group_of(proxy)
                    if group is not None:
                        parked = False
                        if affinity is not None and settings.affinity_enabled:
                            try:
                                await affinity.evacuate(
                                    group, quota_until=until, exclude=tried)
                                parked = True
                            except Exception:
                                logger.exception(
                                    "Affinity evacuation failed; parking group only")
                        if not parked:
                            manager.mark_quota_exhausted(group, until)
                        logger.warning(
                            "Proxy %s got FreeUsageLimitError: quota bucket %s parked "
                            "for %.0fs (until %.0f), sessions evacuated",
                            mask_proxy(proxy.url), group.host, wait, until,
                        )
                    continue
                # Any other 429 (provider/key/per-minute/unknown) is not a
                # daily-quota verdict for this proxy: pass it through without
                # cooldown, parking or rotation.
                logger.warning(
                    "Proxy %s got HTTP 429 (type=%s) — passing through without parking",
                    mask_proxy(proxy.url), opencode.error_type(upstream_body_429))
                proxy_mask = mask_proxy(proxy.url)
                _log_request(key_id=key.key_id, method=request.method, path=rest,
                             model=model, status=status, started=started,
                             stream=stream, account=proxy_mask,
                             error="upstream HTTP 429")
                return Response(content=upstream_body_429, status_code=429,
                                headers=_resp_headers(upstream))
            if status in (401, 403):
                # A body-level FreeTierError verdict ("this request does not
                # come from OpenCode") is identical through every proxy: it is
                # the request, not the egress, that got rejected. Retrying would
                # be pointless — worse, counting it as a proxy failure cools
                # healthy proxies down and can park the whole pool. Pass the
                # upstream verdict through to the client instead.
                upstream_body_403 = await upstream.aread()
                await upstream.aclose()
                rejection = opencode.free_tier_rejection(upstream_body_403)
                if rejection is not None:
                    logger.warning("Upstream rejected the request (HTTP %d): %s",
                                   status, rejection)
                    _log_request(key_id=key.key_id, method=request.method, path=rest,
                                 model=model, status=status, started=started,
                                 stream=stream, account=mask_proxy(proxy.url),
                                 error=rejection)
                    return _openai_error(status, rejection, "upstream_error")
                if status == 403 and opencode.is_abusive_ban(upstream_body_403):
                    # Abusive-traffic ban applies to this proxy's bucket for
                    # ever: persist it, evacuate its sessions and retry this
                    # request on a different proxy.
                    group = manager.group_of(proxy)
                    if group is not None:
                        banned = False
                        if affinity is not None and settings.affinity_enabled:
                            try:
                                await affinity.evacuate(
                                    group, permanent_ban=True, exclude=tried)
                                banned = True
                            except Exception:
                                logger.exception(
                                    "Affinity evacuation failed; banning group only")
                        if not banned:
                            manager.mark_permanently_banned(group)
                        logger.warning(
                            "Proxy %s got abusive-traffic 403: bucket %s "
                            "permanently banned, sessions evacuated",
                            mask_proxy(proxy.url), group.host,
                        )
                    else:
                        manager.mark_failure(proxy, "upstream abusive traffic 403")
                    continue
                # Not a permanent death (proxies are never permanently dead):
                # record a failure and try the next proxy.
                manager.mark_failure(proxy, f"upstream HTTP {status}")
                logger.warning("Proxy %s got HTTP %d, retrying next proxy",
                               mask_proxy(proxy.url), status)
                continue
            if status >= 500:
                await _drain(upstream, True)
                manager.mark_failure(proxy, f"upstream HTTP {status}")
                logger.warning("Proxy %s got HTTP %d, retrying next proxy",
                               mask_proxy(proxy.url), status)
                left = deadline - time.monotonic()
                if left > 0:
                    backoff = settings.retry_backoff_base_seconds * (2 ** (attempt - 1))
                    await asyncio.sleep(min(backoff, left))
                continue
            break  # 2xx/4xx is the client's answer (400 etc.), NOT a proxy error

        proxy_mask = mask_proxy(proxy.url)

        if chat and stream:
            # Caller asked for SSE: pass the upstream SSE through verbatim.
            return StreamingResponse(
                _stream_gen(upstream, proxy, key, request.method, rest, model, started,
                            lease=lease),
                status_code=upstream.status_code,
                headers=_resp_headers(upstream),
                background=BackgroundTask(upstream.aclose),
            )
        if chat and not stream:
            # Non-stream caller: buffer the upstream SSE and convert to a
            # standard OpenAI non-stream chat.completion JSON object.
            content = await upstream.aread()
            await upstream.aclose()
            completion, usage = _aggregate_sse(content, model)
            prompt = completion_log = total = None
            if usage:
                p = usage.get("prompt_tokens")
                c = usage.get("completion_tokens")
                t = usage.get("total_tokens")
                prompt = p if isinstance(p, int) and not isinstance(p, bool) else None
                completion_log = c if isinstance(c, int) and not isinstance(c, bool) else None
                total = t if isinstance(t, int) and not isinstance(t, bool) else None
            # Affinity counts ONLY real upstream usage, never the bytes/4
            # estimate used below for client/TPM compatibility.
            real_total = total
            if real_total is None and prompt is not None and completion_log is not None:
                real_total = prompt + completion_log
            if total is None:
                est = len(content) // 4
                if completion_log is None:
                    completion_log = est
                total = (prompt or 0) + (completion_log or 0)
            msg_content = completion["choices"][0]["message"]["content"]
            if completion_log is None:
                completion_log = len(msg_content.encode("utf-8")) // 4
            completion["usage"] = {
                "prompt_tokens": prompt or 0,
                "completion_tokens": completion_log or 0,
                "total_tokens": max(total or 0, (prompt or 0) + (completion_log or 0)),
            }
            manager.mark_success(proxy)
            if lease is not None and affinity is not None:
                try:
                    affinity.note_tokens(lease, real_total or 0)
                except Exception:
                    logger.exception("Affinity token accounting failed; request continues")
            if total:
                keystore.count_tokens(key, total)
            _log_request(key_id=key.key_id, method=request.method, path=rest, model=model,
                         status=upstream.status_code, started=started, stream=False,
                         account=proxy_mask, prompt=prompt, completion=completion_log)
            return JSONResponse(completion, status_code=upstream.status_code)

        # Non-chat endpoints: buffer and return as-is (or stream if asked).
        if stream:
            return StreamingResponse(
                _stream_gen(upstream, proxy, key, request.method, rest, model, started,
                            stream_usage=False, lease=lease),
                status_code=upstream.status_code,
                headers=_resp_headers(upstream),
                background=BackgroundTask(upstream.aclose),
            )
        content = await upstream.aread()
        await upstream.aclose()
        manager.mark_success(proxy)
        if lease is not None and affinity is not None:
            try:
                affinity.note_tokens(lease, 0)  # non-chat: usage unknown -> 0
            except Exception:
                logger.exception("Affinity token accounting failed; request continues")
        _log_request(key_id=key.key_id, method=request.method, path=rest, model=model,
                     status=upstream.status_code, started=started, stream=False,
                     account=proxy_mask)
        return Response(
            content=content,
            status_code=upstream.status_code,
            headers=_resp_headers(upstream),
        )
    except HTTPException as exc:
        _log_request(key_id=key.key_id, method=request.method, path=rest, model=model,
                     status=exc.status_code, started=started, stream=stream,
                     account=proxy_mask, error=str(exc.detail))
        raise
    except AuthError as exc:
        _log_request(key_id=key.key_id, method=request.method, path=rest, model=model,
                     status=exc.status_code, started=started, stream=stream,
                     account=proxy_mask, error=exc.detail)
        raise
    except RateLimitError as exc:
        _log_request(key_id=key.key_id, method=request.method, path=rest, model=model,
                     status=429, started=started, stream=stream,
                     account=proxy_mask, error=exc.detail)
        raise
    except Exception as exc:
        _log_request(key_id=key.key_id, method=request.method, path=rest, model=model,
                     status=500, started=started, stream=stream,
                     account=proxy_mask, error=f"{type(exc).__name__}: {exc}")
        raise


# --- routes: admin ---------------------------------------------------------


async def admin_status(_: KeyInfo = Depends(require_admin)) -> dict:
    """GET ``/admin/status`` — pool snapshot + affinity + keys + usage summary."""
    st = manager.status()
    st["affinity"] = affinity.status() if affinity is not None else {
        "sessions_total": 0, "sessions_bound": 0, "sessions_unbound": 0,
        "egress_groups": [],
    }
    st["keys"] = keystore.key_ids()
    st["usage"] = await reqlog.summary_async()
    return st


async def admin_logs(limit: int = 100, _: KeyInfo = Depends(require_admin)) -> list[dict]:
    """GET ``/admin/logs?limit=N`` — recent requests (clamped 1..500)."""
    return await reqlog.recent_async(max(1, min(500, limit)))


async def health() -> dict:
    """GET ``/health`` — liveness + pool/affinity counters (additive keys)."""
    st = manager.status()
    aff = affinity.status() if affinity is not None else {}
    return {
        "status": "ok",
        "provider": PROVIDER_NAME,
        "proxies_total": st["proxies_total"],
        "proxies_enabled": st["proxies_enabled"],
        "proxies_manually_disabled": st["proxies_manually_disabled"],
        "proxies_cooling": st["proxies_cooling"],
        "proxies_rpm_capped": st["proxies_rpm_capped"],
        "last_proxies_fetch": st["last_proxies_fetch"],
        # Additive egress-group/session counters (existing keys unchanged).
        "egress_groups_total": st["egress_groups_total"],
        "egress_groups_ready": st["egress_groups_ready"],
        "egress_groups_quota_parked": st["egress_groups_quota_parked"],
        "egress_groups_banned": st["egress_groups_banned"],
        "sessions_bound": aff.get("sessions_bound", 0),
        "sessions_unbound": aff.get("sessions_unbound", 0),
    }


# --- internal API (web-admin only; X-Internal-Token) -------------------
#
# Panel logic (sessions, /login, forms, rendering) lives in the separate
# web-admin; it calls these endpoints for data and mutations. Access is
# ONLY via the X-Internal-Token header (shared secret in both services'
# env, constant-time compare). All endpoints are disabled (404) when the token
# is unset. Edge (nginx) never routes /internal/* at all — a second layer.


def _require_internal(request: Request) -> None:
    """404 without a token ("admin off"), 403 with a wrong one."""
    if not settings.internal_api_token:
        raise HTTPException(404, "Internal API disabled (set INTERNAL_API_TOKEN)")
    got = request.headers.get("x-internal-token", "")
    if not hmac.compare_digest(got.encode("utf-8"),
                               settings.internal_api_token.encode("utf-8")):
        raise HTTPException(403, "Invalid internal token")


def _require_proxydb() -> ProxyDB:
    """ProxyDB is initialised in lifespan; without it (no startup) -> 503."""
    if proxydb is None:
        raise HTTPException(503, "Proxy DB not initialized")
    return proxydb


def _require_keydb() -> KeyDB:
    """KeyDB is initialised in lifespan; without it (no startup) -> 503."""
    if keydb is None:
        raise HTTPException(503, "Key DB not initialized")
    return keydb


def _json_limit(raw: object, field: str) -> tuple[int | None, str | None]:
    """rpm/tpm from JSON: (value, None) or (None, error message).

    None/empty -> None = unlimited; garbage/negative -> error.
    """
    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, int) or isinstance(raw, bool):
        try:
            v = int(str(raw))
        except ValueError:
            return None, f"Invalid {field.upper()}: {raw}"
    else:
        v = raw
    if v < 0:
        return None, f"{field.upper()} cannot be negative"
    return v, None


_PANEL_PERIOD_SECONDS = {
    "day": 24 * 60 * 60.0,
    "week": 7 * 24 * 60 * 60.0,
    "month": 30 * 24 * 60 * 60.0,
}


def _period_since(period: str) -> float | None:
    """Rolling-window cutoff for the admin spending filter; ``None`` = all time."""
    seconds = _PANEL_PERIOD_SECONDS.get((period or "").lower())
    return None if seconds is None else time.time() - seconds


async def _proxies_panel_data(period: str = "all") -> dict:
    """Snapshot for the "Proxies" page: awaits first (the pool may change from
    another tab's POST), then ``status()`` and the zip over ``manager.proxies``
    — so the table and counters are consistent. ``period`` filters the request
    log aggregates (day/week/month = rolling 24h/7d/30d)."""
    since = _period_since(period)
    per_proxy = await reqlog.summary_by_account_async(since)
    usage = await reqlog.summary_async(since)
    recent = await reqlog.recent_async(50)
    st = manager.status()
    rows = []
    for p, s in zip(manager.proxies, st["proxies"]):
        s2 = dict(s)
        s2["real_proxy"] = p.url  # only in hidden form fields; the screen shows the mask
        rows.append(s2)
    st["proxies"] = rows
    return {"status": st, "usage": usage, "usage_by_proxy": per_proxy, "recent": recent}


async def _keys_panel_data(period: str = "all") -> dict:
    """Snapshot for the "Keys" page: key config from keydb + per-key stats from
    the request log (including deleted keys — it is keyed by key_id)."""
    usage = await reqlog.summary_async(_period_since(period))
    keys_rows = await asyncio.to_thread(_require_keydb().all)
    return {"keys": keys_rows, "usage": usage}


async def internal_proxies_data(request: Request, period: str = "all") -> dict:
    """GET ``/internal/proxies-data?period=...`` — snapshot for the admin panel."""
    _require_internal(request)
    return await _proxies_panel_data(period)


async def internal_keys_data(request: Request, period: str = "all") -> dict:
    """GET ``/internal/keys-data?period=...`` — key table + per-key stats."""
    _require_internal(request)
    return await _keys_panel_data(period)


async def internal_proxies_refresh(request: Request) -> dict:
    """POST ``/internal/proxies/refresh`` - force a cache-busting refresh.

    The admin panel uses this when an operator asks for the current px6.net
    list; ``?refresh=true`` tells px6-service to ignore its TTL cache. The
    regular background loop keeps using the cached endpoint.
    """
    _require_internal(request)
    try:
        ok = await manager.refresh_proxies(bust_cache=True)
    except Exception as exc:
        logger.exception("Manual proxy refresh crashed")
        raise HTTPException(502, "Proxy refresh failed") from exc
    if not ok:
        raise HTTPException(502, "Proxy refresh failed")
    return {"ok": True, "message": "Proxies refreshed"}


async def internal_proxies_set(request: Request) -> dict:
    """``{"proxy": str, "enabled": bool}`` — manual on/off of a proxy."""
    _require_internal(request)
    data = await _read_json(request)
    proxy = str(data.get("proxy", ""))
    enabled = bool(data.get("enabled"))
    db = _require_proxydb()
    if await asyncio.to_thread(db.set_enabled, proxy, enabled):
        manager.set_manually_disabled(proxy, not enabled)
        if not enabled and affinity is not None:
            try:
                affinity.forget_proxy(proxy)  # invalidate stale sticky bindings
            except Exception:
                logger.exception("Affinity forget_proxy failed for %s", mask_proxy(proxy))
        return {"ok": True, "message": "Proxy enabled" if enabled else "Proxy disabled"}
    raise HTTPException(404, "Proxy not found")


async def internal_keys_add(request: Request) -> dict:
    """``{"key": plaintext, "key_id"?, "name"?, "admin"?, "rpm"?, "tpm"?,
    "models"?}``. Plaintext is hashed immediately (keys.py) and never stored."""
    _require_internal(request)
    data = await _read_json(request)
    plaintext = str(data.get("key", "")).strip()
    if not plaintext:
        return {"ok": False, "message": "No key given"}
    if len(plaintext) > 200:
        return {"ok": False, "message": "Key too long (max 200 chars)"}
    rpm, err = _json_limit(data.get("rpm"), "rpm")
    if err:
        return {"ok": False, "message": err}
    tpm, err = _json_limit(data.get("tpm"), "tpm")
    if err:
        return {"ok": False, "message": err}
    raw_models = data.get("models")
    if isinstance(raw_models, list):
        models = [str(m).strip() for m in raw_models if str(m).strip()] or None
    elif isinstance(raw_models, str):
        models = [m.strip() for m in raw_models.split(",") if m.strip()] or None
    else:
        models = None
    is_admin = bool(data.get("admin"))
    name = str(data.get("name", "")).strip()
    key_id = str(data.get("key_id", "")).strip()
    if not key_id:
        # auto-generate without colliding with existing key_ids
        existing = set(keystore.key_ids())
        i = 1
        while f"k{i}" in existing:
            i += 1
        key_id = f"k{i}"
    h = hash_key(plaintext)
    db = _require_keydb()
    if await asyncio.to_thread(db.add, h, key_id, name or key_id,
                               admin=is_admin, rpm=rpm, tpm=tpm, models=models):
        keystore.upsert(h, KeyInfo(
            key_id=key_id, name=name or key_id,
            is_admin=is_admin, rpm=rpm, tpm=tpm, models=models, enabled=True,
        ))
        return {"ok": True, "message": f"Key {key_id} added"}
    return {"ok": False, "message": "Such a key already exists"}


async def internal_keys_set(request: Request) -> dict:
    """``{"key_hash": str, "enabled": bool}`` — revoke/enable a key."""
    _require_internal(request)
    data = await _read_json(request)
    key_hash = str(data.get("key_hash", ""))
    enabled = bool(data.get("enabled"))
    db = _require_keydb()
    if await asyncio.to_thread(db.set_enabled, key_hash, enabled):
        keystore.set_enabled(key_hash, enabled)
        return {"ok": True, "message": "Key enabled" if enabled else "Key revoked (401)"}
    raise HTTPException(404, "Key not found")


async def internal_keys_delete(request: Request) -> dict:
    _require_internal(request)
    data = await _read_json(request)
    key_hash = str(data.get("key_hash", ""))
    db = _require_keydb()
    if await asyncio.to_thread(db.remove, key_hash):
        keystore.remove(key_hash)
        return {"ok": True, "message": "Key deleted"}
    raise HTTPException(404, "Key not found")


async def _read_json(request: Request) -> dict:
    """Small JSON body of an internal request (anti-DoS: 64 KiB)."""
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(413, "Request body too large")
    if not body:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON body must be an object")
    return data


# --- app factory -----------------------------------------------------------


def create_app() -> FastAPI:
    """Build and return the FastAPI application (routes + handlers wired)."""
    application = FastAPI(
        title=f"{PROVIDER_NAME} API",
        description=f"OpenAI-compatible API gateway {PROVIDER_NAME}: a pool of "
                    "HTTP(S) proxies round-robined onto opencode.ai free-tier inference.",
        version="2.0.0",
        lifespan=lifespan,
    )
    application.add_api_route(
        "/v1/{rest:path}", proxy_v1,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        summary="OpenAI-compatible passthrough (chat/completions, models, ...)",
    )
    application.add_api_route("/admin/status", admin_status, methods=["GET"])
    application.add_api_route("/admin/logs", admin_logs, methods=["GET"])
    application.add_api_route("/health", health, methods=["GET"])
    application.add_api_route(f"{_INTERNAL_PREFIX}/proxies-data", internal_proxies_data,
                              methods=["GET"], include_in_schema=False)
    application.add_api_route(f"{_INTERNAL_PREFIX}/keys-data", internal_keys_data,
                              methods=["GET"], include_in_schema=False)
    application.add_api_route(f"{_INTERNAL_PREFIX}/proxies/refresh", internal_proxies_refresh,
                              methods=["POST"], include_in_schema=False)
    application.add_api_route(f"{_INTERNAL_PREFIX}/proxies/set", internal_proxies_set,
                              methods=["POST"], include_in_schema=False)
    application.add_api_route(f"{_INTERNAL_PREFIX}/keys/add", internal_keys_add,
                              methods=["POST"], include_in_schema=False)
    application.add_api_route(f"{_INTERNAL_PREFIX}/keys/set", internal_keys_set,
                              methods=["POST"], include_in_schema=False)
    application.add_api_route(f"{_INTERNAL_PREFIX}/keys/delete", internal_keys_delete,
                              methods=["POST"], include_in_schema=False)
    application.add_exception_handler(StarletteHTTPException, openai_error_handler)
    application.add_exception_handler(AuthError, auth_error_handler)
    application.add_exception_handler(RateLimitError, rate_limit_error_handler)
    return application


app = create_app()

"""HTTP client for the private ``storage-service`` API.

The gateway never talks to PostgreSQL: ``storage-service`` owns all SQL and
exposes a small JSON API (see ``storage-service/README.md``). This module is
the gateway's typed façade over that API — one method per storage domain
(proxies, keys, affinity, personas, request log/stats).

Design notes:

- **No import-time network**. The underlying ``httpx.AsyncClient`` is created
  in ``__init__`` (never at import time); tests inject either a ``transport``
  (``httpx.MockTransport``) or a pre-built ``client``.
- **No SQL / no DSN**. The client knows only an HTTP base URL and the shared
  ``X-Storage-Token`` header.
- **Errors are typed and secret-free**. Response bodies, the token and the DSN
  are never embedded in exception messages; callers map the exception class,
  not response text. Transport errors, timeouts and 5xx are
  :class:`StorageUnavailable`; 401/403 are :class:`StorageAuthError`; 404 is
  :class:`StorageNotFound`; 409 is :class:`StorageConflict`; every other
  non-2xx is :class:`StorageAPIError` carrying only the status code.
- **No retries**. Retry policy belongs to the caller (the gateway already has
  one for upstream calls); an HTTP storage call is not silently repeated.
- **Ownership**: the client owns its ``httpx.AsyncClient`` even when one is
  injected; ``aclose()`` (and the async context manager) closes it.

The service itself is private (no edge route, no host port), so the base URL
will normally be the container name from ``docker-compose.yml``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps the client lightweight
    from .reqlog import LogEntry

__all__ = [
    "RECENT_LIMIT_MAX",
    "RECENT_LIMIT_MIN",
    "StorageAPIError",
    "StorageAuthError",
    "StorageClient",
    "StorageConflict",
    "StorageError",
    "StorageNotFound",
    "StorageUnavailable",
]

#: Service-side validation bounds for ``GET /request-log/recent``.
RECENT_LIMIT_MIN = 1
RECENT_LIMIT_MAX = 1000


# --- errors -----------------------------------------------------------------


class StorageError(Exception):
    """Base class for every storage-service failure.

    ``status_code`` is the upstream HTTP status when one exists (``None`` for
    transport failures and a closed client). Messages are deliberately generic:
    no response body, token or DSN is ever included.
    """

    def __init__(self, message: str = "storage operation failed",
                 *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class StorageUnavailable(StorageError):
    """Transport error, timeout, 5xx or degraded service (caller: 503)."""


class StorageAuthError(StorageError):
    """Wrong/missing ``X-Storage-Token`` or a disabled authenticated API.

    Treated like :class:`StorageUnavailable` by callers: the gateway cannot
    persist anything until its configuration is fixed, so the public surface
    degrades instead of leaking details.
    """


class StorageNotFound(StorageError):
    """The requested row does not exist (404)."""


class StorageConflict(StorageError):
    """The write conflicts with existing state (409)."""


class StorageAPIError(StorageError):
    """Any other non-2xx answer; only status + a generic message are kept."""

    def __init__(self, status_code: int,
                 message: str = "storage request failed") -> None:
        self.message = message
        super().__init__(f"{message} (HTTP {status_code})",
                         status_code=status_code)


# --- internal response helpers ---------------------------------------------


@dataclass(frozen=True)
class _Reply:
    """A parsed 2xx response: status + JSON body (kept private)."""

    status_code: int
    data: Any


def _unexpected(reply: _Reply) -> StorageAPIError:
    return StorageAPIError(reply.status_code,
                           "storage service returned an unexpected response")


def _object(reply: _Reply) -> dict[str, Any]:
    if not isinstance(reply.data, dict):
        raise _unexpected(reply)
    return reply.data


def _rows(reply: _Reply, field: str) -> list[dict[str, Any]]:
    data = _object(reply)
    rows = data.get(field)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise _unexpected(reply)
    return rows


def _bool_field(reply: _Reply, field: str) -> bool:
    value = _object(reply).get(field)
    if not isinstance(value, bool):
        raise _unexpected(reply)
    return value


def _int_field(reply: _Reply, field: str) -> int:
    value = _object(reply).get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _unexpected(reply)
    return value


# --- payload normalisation --------------------------------------------------

_PERSONA_FIELDS = (
    "quota_key", "session_id", "project_id", "trace_id", "created_at",
    "generation",
)

BindingInput = Mapping[str, Any] | Sequence[Any]


def _binding_payload(row: BindingInput) -> dict[str, Any]:
    """Normalise one binding (mapping or a ``(key, proxy, created, used)`` tuple).

    The affinity writer uses tuples internally; the storage API uses named
    fields. Accepting both keeps the adapter independent of the wire shape.
    """
    if isinstance(row, Mapping):
        session_key = row["session_key"]
        proxy = row.get("proxy")
        created_at = row["created_at"]
        last_used_at = row["last_used_at"]
    else:
        session_key, proxy, created_at, last_used_at = row
    return {
        "session_key": session_key,
        "proxy": proxy,
        "created_at": float(created_at),
        "last_used_at": float(last_used_at),
    }


def _persona_payload(persona: Any) -> dict[str, Any]:
    """Normalise a persona mapping or a duck-typed ``Persona`` dataclass."""
    if isinstance(persona, Mapping):
        source = persona
    else:
        source = {name: getattr(persona, name) for name in _PERSONA_FIELDS}
    return {
        "quota_key": source["quota_key"],
        "session_id": source["session_id"],
        "project_id": source["project_id"],
        "trace_id": source["trace_id"],
        "created_at": float(source["created_at"]),
        "generation": int(source["generation"]),
    }


def _log_payload(entry: "LogEntry | Mapping[str, Any]") -> dict[str, Any]:
    """Convert a ``LogEntry`` (or mapping) to the service's exact 12 fields."""
    if is_dataclass(entry) and not isinstance(entry, type):
        data: Mapping[str, Any] = asdict(entry)
    elif isinstance(entry, Mapping):
        data = entry
    else:  # pragma: no cover - defensive, the gateway always passes LogEntry
        raise TypeError("log entry must be a LogEntry dataclass or a mapping")
    return {
        "ts": float(data["ts"]),
        "key_id": data["key_id"],
        "method": data["method"],
        "path": data["path"],
        "model": data.get("model"),
        "status": int(data["status"]),
        "latency_ms": float(data["latency_ms"]),
        "stream": bool(data.get("stream", False)),
        "account": data.get("account"),
        "prompt_tokens": data.get("prompt_tokens"),
        "completion_tokens": data.get("completion_tokens"),
        "error": data.get("error"),
    }


# --- client -----------------------------------------------------------------


class StorageClient:
    """Typed async client for ``storage-service``.

    ``base_url`` is the service root (``http://openroute-storage:8000`` inside
    compose), ``token`` is ``STORAGE_API_TOKEN``. ``timeout`` applies to every
    request. Pass ``transport=httpx.MockTransport(handler)`` (or a fully built
    ``client``) in tests; no connection is opened at import time.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float | httpx.Timeout | None = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._closed = False
        self._client = client if client is not None else httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
        )

    # --- lifecycle --------------------------------------------------------

    @property
    def closed(self) -> bool:
        """True once ``aclose()`` ran (or the inner client was closed)."""
        return self._closed or self._client.is_closed

    async def aclose(self) -> None:
        """Close the underlying HTTP client. Idempotent."""
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    async def __aenter__(self) -> "StorageClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # --- health -----------------------------------------------------------

    async def check(self) -> dict[str, Any]:
        """Verify reachability and token validity (startup/health use).

        Calls the unauthenticated ``GET /health`` (so a database outage is
        distinguishable from a bad token) and then the authenticated
        ``GET /proxies?listed=true``. Returns a small summary; any failure
        raises a typed :class:`StorageError` whose message contains neither
        the token nor the DSN.
        """
        health = await self._request("GET", "/health", auth=False)
        data = _object(health)
        if data.get("status") != "ok" or data.get("database") != "ok":
            raise StorageUnavailable("storage service is degraded",
                                     status_code=health.status_code)
        rows = await self.list_proxies(listed=True)
        return {
            "status": "ok",
            "database": "ok",
            "proxies": len(rows),
        }

    async def ping(self) -> bool:
        """``True`` when :meth:`check` succeeds; never raises a storage error."""
        try:
            await self.check()
        except StorageError:
            return False
        return True

    # --- low-level request -------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        auth: bool = True,
    ) -> _Reply:
        if self._closed:
            raise StorageUnavailable("storage client is closed")
        headers = {"Accept": "application/json"}
        if auth and self._token:
            headers["X-Storage-Token"] = self._token

        try:
            response = await self._client.request(
                method,
                self._url(path),
                params=params,
                json=json,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # Never chain the httpx exception: its repr may include the URL.
            raise StorageUnavailable(
                f"storage service unreachable ({type(exc).__name__})"
            ) from None

        status = response.status_code
        if status >= 500:
            raise StorageUnavailable(
                f"storage service unavailable (HTTP {status})",
                status_code=status,
            )
        if status in (401, 403):
            raise StorageAuthError(
                f"storage authentication failed (HTTP {status})",
                status_code=status,
            )
        if status == 404:
            raise StorageNotFound(
                f"storage resource not found (HTTP {status})",
                status_code=status,
            )
        if status == 409:
            raise StorageConflict(
                f"storage conflict (HTTP {status})",
                status_code=status,
            )
        if not 200 <= status < 300:
            raise StorageAPIError(status)

        try:
            data = response.json()
        except ValueError:
            raise StorageAPIError(
                status, "storage service returned invalid JSON"
            ) from None
        return _Reply(status_code=status, data=data)

    # --- proxies ----------------------------------------------------------

    async def sync_proxies(self, urls: Iterable[str]) -> dict[str, Any]:
        """Apply one px6 catalogue snapshot; returns ``{added, proxies}``."""
        reply = await self._request(
            "POST", "/proxies/sync", json={"proxies": list(urls)}
        )
        return _object(reply)

    async def list_proxies(self, listed: bool | None = None) -> list[dict[str, Any]]:
        """Proxy rows; ``listed=None`` includes soft-removed rows."""
        params = None if listed is None else {"listed": listed}
        reply = await self._request("GET", "/proxies", params=params)
        return _rows(reply, "proxies")

    async def set_proxy_enabled(self, proxy: str, enabled: bool) -> bool:
        """Manual on/off; returns ``updated`` or raises ``StorageNotFound``."""
        reply = await self._request(
            "POST",
            "/proxies/set-enabled",
            json={"proxy": proxy, "enabled": enabled},
        )
        return _bool_field(reply, "updated")

    async def save_parking(
        self,
        proxies: Iterable[str],
        quota_until: float,
        banned_until: float,
        permanently_banned: bool,
    ) -> int:
        """Persist quota parking / timed ban / permanent abusive ban."""
        reply = await self._request(
            "POST",
            "/proxies/parking",
            json={
                "proxies": list(proxies),
                "quota_until": quota_until,
                "banned_until": banned_until,
                "permanently_banned": permanently_banned,
            },
        )
        return _int_field(reply, "updated")

    # --- keys -------------------------------------------------------------

    async def list_keys(self) -> list[dict[str, Any]]:
        reply = await self._request("GET", "/keys")
        return _rows(reply, "keys")

    async def add_key(
        self,
        key_hash: str,
        key_id: str,
        rpm: int | None = None,
        tpm: int | None = None,
        models: list[str] | None = None,
    ) -> bool:
        """Insert a key by hash; ``False`` = duplicate hash (no-op)."""
        reply = await self._request(
            "POST",
            "/keys",
            json={
                "key_hash": key_hash,
                "key_id": key_id,
                "rpm": rpm,
                "tpm": tpm,
                "models": models,
            },
        )
        return _bool_field(reply, "created")

    async def rotate_key(self, old_hash: str, new_hash: str) -> dict[str, Any]:
        """Replace a key hash in place; the service makes retries idempotent."""
        reply = await self._request(
            "POST",
            "/keys/rotate",
            json={"key_hash": old_hash, "new_key_hash": new_hash},
        )
        return _object(reply)

    async def set_key_enabled(self, key_hash: str, enabled: bool) -> bool:
        reply = await self._request(
            "POST",
            "/keys/set-enabled",
            json={"key_hash": key_hash, "enabled": enabled},
        )
        return _bool_field(reply, "updated")

    async def delete_key(self, key_hash: str) -> bool:
        reply = await self._request("DELETE", f"/keys/{key_hash}")
        return _bool_field(reply, "deleted")

    # --- affinity ---------------------------------------------------------

    async def load_affinity(self) -> list[dict[str, Any]]:
        reply = await self._request("GET", "/affinity")
        return _rows(reply, "bindings")

    async def upsert_affinity(
        self, rows: Iterable[BindingInput]
    ) -> dict[str, Any]:
        """Upsert bindings; returns ``{written, unbound}``."""
        bindings = [_binding_payload(row) for row in rows]
        reply = await self._request(
            "POST", "/affinity", json={"bindings": bindings}
        )
        return _object(reply)

    async def clear_affinity(self, session_keys: Iterable[str]) -> int:
        reply = await self._request(
            "POST", "/affinity/clear",
            json={"session_keys": list(session_keys)},
        )
        return _int_field(reply, "cleared")

    # --- personas ---------------------------------------------------------

    async def load_personas(self) -> list[dict[str, Any]]:
        reply = await self._request("GET", "/personas")
        return _rows(reply, "personas")

    async def get_persona(self, quota_key: str) -> dict[str, Any] | None:
        """One persona; ``None`` when the bucket has none.

        The quota key is a masked ``host:port`` (IPv6 brackets included), so it
        is percent-encoded as a single path segment.
        """
        encoded = quote(quota_key, safe="")
        try:
            reply = await self._request("GET", f"/personas/{encoded}")
        except StorageNotFound:
            return None
        return _object(reply)

    async def upsert_persona(self, persona: Any) -> bool:
        """Write one persona; ``False`` when the generation guard rejected it."""
        reply = await self._request(
            "POST", "/personas", json=_persona_payload(persona)
        )
        return _bool_field(reply, "written")

    # --- request log / stats ---------------------------------------------

    async def log_request(self, entry: "LogEntry | Mapping[str, Any]") -> None:
        """Append one request-log row (the payload is committed on return)."""
        await self._request("POST", "/request-log", json=_log_payload(entry))

    async def recent_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        reply = await self._request(
            "GET", "/request-log/recent", params={"limit": limit}
        )
        return _rows(reply, "entries")

    async def stats_summary(self, since: float | None = None) -> dict[str, Any]:
        """Per-key aggregates + ``total`` (``since`` is an epoch cutoff)."""
        params = None if since is None else {"since": since}
        reply = await self._request("GET", "/stats/summary", params=params)
        return _object(reply)

    async def stats_by_account(self, since: float | None = None) -> dict[str, Any]:
        """Per-masked-proxy aggregates; ``since`` omits older rows."""
        params = None if since is None else {"since": since}
        reply = await self._request("GET", "/stats/by-account", params=params)
        return _object(reply)

    async def stats_daily(
        self, since: float | None = 0.0
    ) -> list[dict[str, Any]]:
        """Per-UTC-day/per-account series, sorted by ``(day, account)``."""
        params = None if since is None else {"since": since}
        reply = await self._request("GET", "/stats/daily", params=params)
        if not isinstance(reply.data, list) or not all(
            isinstance(row, dict) for row in reply.data
        ):
            raise _unexpected(reply)
        return reply.data

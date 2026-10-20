"""Client API keys: auth, model allow-list, RPM/TPM limits.

Near-verbatim port of ``NVRoute/app/keys.py`` — the client-facing contract is
unchanged; only the provider string in error messages changes (``OpenRoute``).

Keys live in PostgreSQL (table ``keys``, module ``keydb.py``) and are managed
only via the web admin UI. In memory only sha256 hashes + config (``KeyInfo``)
are held; plaintext is never stored or logged. Comparisons are constant-time
(``hmac.compare_digest``) over sha256 digests. RPM/TPM counters are in-memory
sliding 60s windows (single event loop / single worker): with ``--workers N``
or on restart, counters reset and are not shared.

Enforcement:
- RPM — ``check_rate(key)`` BEFORE proxying the request;
- TPM — ``check_tpm(key|key_id)`` BEFORE proxying the request (gate);
  ``count_tokens(key, n)`` AFTER the response (usage recorder, never raises;
  a boundary overshoot is recorded and the limit bites the NEXT request).
Both errors are ``RateLimitError`` with ``retry_after`` (seconds until the
window frees up).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque
from dataclasses import dataclass

_WINDOW = 60.0  # RPM/TPM window, seconds


def hash_key(key: str) -> str:
    """sha256 hex digest of a plaintext key (the only form ever persisted)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class AuthError(Exception):
    """Auth failure (401) or forbidden model (403); main.py maps to HTTP."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class RateLimitError(Exception):
    """RPM/TPM limit exceeded (429). ``retry_after`` = seconds until headroom."""

    def __init__(self, detail: str, retry_after: float | None = None) -> None:
        self.detail = detail
        self.retry_after = retry_after
        super().__init__(detail)


@dataclass
class KeyInfo:
    key_id: str
    name: str
    is_admin: bool
    rpm: int | None  # requests/min, None = unlimited
    tpm: int | None  # tokens/min, None = unlimited
    models: list[str] | None  # allow-list, None = all
    enabled: bool


class KeyStore:
    """In-memory key store (sha256 hashes + config, no plaintext)."""

    def __init__(self) -> None:
        self._by_hash: dict[str, KeyInfo] = {}
        self._by_id: dict[str, KeyInfo] = {}  # key_id -> KeyInfo (for check_tpm)
        self._rpm: dict[str, deque[float]] = {}  # key_id -> request timestamps
        self._tpm: dict[str, deque[tuple[float, int]]] = {}  # key_id -> (time, tokens)

    # --- load / mutations ---

    def load(self, rows: list[dict]) -> None:
        """Full reload from ``keydb.all()`` (called in lifespan).

        A full reload: RPM/TPM windows reset — they are empty at startup
        anyway. Admin mutations use incremental upsert/remove/set_enabled so
        that other keys' counters are not disturbed.
        """
        by_hash: dict[str, KeyInfo] = {}
        for r in rows:
            info = KeyInfo(
                key_id=r["key_id"],
                name=r["name"] or r["key_id"],
                is_admin=bool(r["admin"]),
                rpm=r["rpm"],
                tpm=r["tpm"],
                models=list(r["models"]) if r["models"] else None,
                enabled=bool(r["enabled"]),
            )
            by_hash[r["key_hash"]] = info
        self._by_hash = by_hash
        # key_id -> KeyInfo; on duplicate key_id the last one wins
        self._by_id = {info.key_id: info for info in by_hash.values()}
        self._rpm.clear()
        self._tpm.clear()

    def upsert(self, key_hash: str, info: KeyInfo) -> bool:
        """Add/update without resetting other keys' rate windows. True = new."""
        existed = key_hash in self._by_hash
        self._by_hash[key_hash] = info
        self._by_id[info.key_id] = info
        return not existed

    def remove(self, key_hash: str) -> bool:
        """Remove a key (and its rate windows). True = existed."""
        info = self._by_hash.pop(key_hash, None)
        if info is None:
            return False
        if self._by_id.get(info.key_id) is info:
            del self._by_id[info.key_id]
        self._rpm.pop(info.key_id, None)
        self._tpm.pop(info.key_id, None)
        return True

    def set_enabled(self, key_hash: str, enabled: bool) -> bool:
        """Revoke/restore without resetting rate windows."""
        info = self._by_hash.get(key_hash)
        if info is None:
            return False
        info.enabled = enabled
        return True

    # --- auth ---

    def authenticate(self, authorization_header: str) -> KeyInfo:
        """Parse ``Bearer <key>``, constant-time hash compare.
        Unknown/missing/revoked -> ``AuthError(401)``.
        """
        scheme, _, key = (authorization_header or "").partition(" ")
        if scheme.lower() != "bearer" or not key:
            raise AuthError(401, "Invalid OpenRoute API key")
        digest = hash_key(key)
        info: KeyInfo | None = None
        for stored, candidate in self._by_hash.items():
            if hmac.compare_digest(stored, digest):
                info = candidate
                break
        if info is None or not info.enabled:
            raise AuthError(401, "Invalid OpenRoute API key")
        return info

    # --- limits ---

    def check_model(self, key: KeyInfo, model: str | None) -> None:
        """Allow-list: ``models is None`` = all; model outside list -> 403."""
        if key.models is not None and model is not None and model not in key.models:
            raise AuthError(403, f"Model {model} not allowed for this key")

    def check_rate(self, key: KeyInfo) -> None:
        """RPM: sliding 60s window; records the request and raises over limit.

        The rejected request still occupies a window slot (appended before the
        check) — preserved behaviour from NVRoute.
        """
        if key.rpm is None:
            return
        now = time.monotonic()
        win = self._rpm.setdefault(key.key_id, deque())
        while win and now - win[0] >= _WINDOW:
            win.popleft()
        win.append(now)
        if len(win) > key.rpm:
            # the next append must yield len <= rpm, i.e. before it <= rpm-1
            # marks may remain: len-(rpm-1) oldest marks must age out.
            drop = len(win) - (key.rpm - 1)
            retry_after = max(0.0, win[drop - 1] + _WINDOW - now)
            raise RateLimitError(
                f"Rate limit exceeded: {key.rpm} req/min", retry_after=retry_after
            )

    def check_tpm(self, key: KeyInfo | str) -> None:
        """TPM pre-request gate: called BEFORE proxying the request.

        Checks that tokens consumed in the sliding 60s window are below the
        key's tpm limit; otherwise ``RateLimitError`` with ``retry_after`` =
        seconds until the window slides far enough to free headroom. Records
        nothing — usage is recorded by ``count_tokens()`` after the response.

        Accepts a ``KeyInfo`` or a ``key_id`` (str). tpm=None/0 or an unknown
        key_id -> unlimited, pass.
        """
        info = self._by_id.get(key) if isinstance(key, str) else key
        if info is None or info.tpm is None or info.tpm <= 0:
            return
        now = time.monotonic()
        win = self._tpm.get(info.key_id)
        if not win:
            return
        while win and now - win[0][0] >= _WINDOW:
            win.popleft()
        total = sum(t for _, t in win)
        if total < info.tpm:
            return
        # how many oldest entries must age out for the sum to drop below tpm
        running = total
        drop = 0
        for _, t in win:
            if running < info.tpm:
                break
            running -= t
            drop += 1
        retry_after = max(0.0, win[drop - 1][0] + _WINDOW - now) if drop else 0.0
        raise RateLimitError(
            f"Token limit exceeded: {info.tpm} tokens/min", retry_after=retry_after
        )

    def count_tokens(self, key: KeyInfo, tokens: int) -> None:
        """TPM post-response recorder: records usage in the sliding 60s window.
        Never raises. A boundary overshoot is still recorded — enforcement
        happens in check_tpm() before the NEXT request.
        Only called when ``key.tpm > 0``.
        """
        if key.tpm is None or key.tpm <= 0 or tokens <= 0:
            return
        now = time.monotonic()
        win = self._tpm.setdefault(key.key_id, deque())
        while win and now - win[0][0] >= _WINDOW:
            win.popleft()
        win.append((now, tokens))

    # --- service ---

    def key_ids(self) -> list[str]:
        return sorted(i.key_id for i in self._by_hash.values())

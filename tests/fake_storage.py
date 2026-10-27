"""In-memory stand-in for :class:`gateway.storage.StorageClient`.

Gateway tests must not need PostgreSQL (or the storage-service HTTP process).
``FakeStorage`` mirrors the storage API's observable semantics closely enough
that a test can assert real behaviour:

- proxy snapshots: new rows start disabled, existing ``enabled``/parking state
  survives, absent rows are soft-removed (``listed = False``), never deleted;
- keys: only hashes are stored, duplicate inserts are no-ops, rotation is
  idempotent (retrying the old/new pair reports ``already_rotated``), and a
  hash collision is a conflict;
- affinity: moving a session preserves ``created_at``; a binding to a vanished
  proxy is stored as ``proxy = None`` (old FK fallback); clearing keeps the row;
- personas: the ``generation <=`` guard rejects stale writes;
- request log/stats: UTC-day buckets and the same aggregates as the SQL.

The fake is test-only. It is importable without a database and is never used by
production code. ``StorageClient`` and the fake expose the same async method
names, so gateway tests can swap one for the other.

Test hooks: :meth:`raise_unavailable` and :meth:`fail_next` make every public
method fail deterministically.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from typing import Any

from gateway.storage import (
    RECENT_LIMIT_MAX,
    RECENT_LIMIT_MIN,
    StorageAPIError,
    StorageConflict,
    StorageError,
    StorageNotFound,
    StorageUnavailable,
)

__all__ = ["FakeStorage"]

_BINDING_FIELDS = ("session_key", "proxy", "created_at", "last_used_at")
_PERSONA_FIELDS = (
    "quota_key", "session_id", "project_id", "trace_id", "created_at",
    "generation",
)
_LOG_FIELDS = (
    "ts", "key_id", "method", "path", "model", "status", "latency_ms",
    "stream", "account", "prompt_tokens", "completion_tokens", "error",
)


def _utc_day(ts: float) -> str:
    bucket = int(ts // 86400) * 86400
    return datetime.fromtimestamp(bucket, tz=timezone.utc).strftime("%Y-%m-%d")


def _is_error(entry: Mapping[str, Any]) -> int:
    status = int(entry.get("status") or 0)
    return 1 if status >= 400 or entry.get("error") is not None else 0


def _tokens(entry: Mapping[str, Any]) -> int:
    return int(entry.get("prompt_tokens") or 0) + int(
        entry.get("completion_tokens") or 0
    )


class FakeStorage:
    """In-memory implementation of the ``StorageClient`` async surface."""

    def __init__(
        self,
        *,
        proxies: Iterable[str | Mapping[str, Any]] = (),
        keys: Iterable[Mapping[str, Any]] = (),
        affinity: Iterable[Mapping[str, Any] | Sequence[Any]] = (),
        personas: Iterable[Mapping[str, Any] | Any] = (),
        log: Iterable[Mapping[str, Any] | Any] = (),
    ) -> None:
        #: ``proxy URL -> row`` in insertion order (the service's ``ORDER BY id``).
        self.proxies: dict[str, dict[str, Any]] = {}
        #: ``key_hash -> row`` in insertion order.
        self.keys: dict[str, dict[str, Any]] = {}
        #: ``session_key -> binding``.
        self.affinity: dict[str, dict[str, Any]] = {}
        #: ``quota_key -> persona``.
        self.personas: dict[str, dict[str, Any]] = {}
        #: Appended request-log rows (insertion order; reads sort by ``ts``).
        self.log: list[dict[str, Any]] = []

        self._unavailable = False
        self._forced: BaseException | None = None
        self._closed = False

        for item in proxies:
            self._seed_proxy(item)
        for item in keys:
            self._seed_key(item)
        for item in affinity:
            self._seed_affinity(item)
        for item in personas:
            self._seed_persona(item)
        for item in log:
            self._seed_log(item)

    # --- test hooks / lifecycle ------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def raise_unavailable(self, enabled: bool = True) -> None:
        """Make every call raise :class:`StorageUnavailable` until disabled."""
        self._unavailable = bool(enabled)

    def fail_unavailable(self, enabled: bool = True) -> None:
        """Alias of :meth:`raise_unavailable`."""
        self.raise_unavailable(enabled)

    def fail_next(self, exc: BaseException) -> None:
        """Raise ``exc`` on the next call, then resume normal operation."""
        self._forced = exc

    def _guard(self) -> None:
        if self._closed:
            raise StorageUnavailable("storage client is closed")
        forced, self._forced = self._forced, None
        if forced is not None:
            raise forced
        if self._unavailable:
            raise StorageUnavailable("storage service unavailable",
                                     status_code=503)

    async def aclose(self) -> None:
        self._closed = True

    async def __aenter__(self) -> "FakeStorage":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def check(self) -> dict[str, Any]:
        self._guard()
        return {
            "status": "ok",
            "database": "ok",
            "proxies": sum(1 for row in self.proxies.values() if row["listed"]),
        }

    async def ping(self) -> bool:
        try:
            await self.check()
        except StorageError:
            return False
        return True

    # --- proxy domain -----------------------------------------------------

    async def sync_proxies(self, urls: Iterable[str]) -> dict[str, Any]:
        self._guard()
        deduped = list(dict.fromkeys(url for url in urls if url))
        known = set(deduped)
        now = time.time()
        added = 0
        for url in deduped:
            if url in self.proxies:
                continue
            self.proxies[url] = {
                "proxy": url,
                "enabled": False,  # new proxies always start disabled
                "added_at": now,
                "quota_until": 0.0,
                "banned_until": 0.0,
                "permanently_banned": False,
                "listed": True,
            }
            added += 1
        for row in self.proxies.values():
            row["listed"] = row["proxy"] in known
        return {
            "added": added,
            "proxies": [dict(row) for row in self.proxies.values()],
        }

    async def list_proxies(
        self, listed: bool | None = None
    ) -> list[dict[str, Any]]:
        self._guard()
        rows = self.proxies.values()
        if listed is not None:
            rows = [row for row in rows if row["listed"] == listed]
        return [dict(row) for row in rows]

    async def set_proxy_enabled(self, proxy: str, enabled: bool) -> bool:
        self._guard()
        row = self.proxies.get(proxy)
        if row is None:
            raise StorageNotFound("proxy not found", status_code=404)
        row["enabled"] = bool(enabled)
        return True

    async def save_parking(
        self,
        proxies: Iterable[str],
        quota_until: float,
        banned_until: float,
        permanently_banned: bool,
    ) -> int:
        self._guard()
        urls = list(dict.fromkeys(url for url in proxies if url))
        updated = 0
        for url in urls:
            row = self.proxies.get(url)
            if row is None:
                continue
            row["quota_until"] = float(quota_until)
            row["banned_until"] = float(banned_until)
            row["permanently_banned"] = bool(
                row["permanently_banned"] or permanently_banned
            )
            updated += 1
        return updated

    # --- key domain -------------------------------------------------------

    async def list_keys(self) -> list[dict[str, Any]]:
        self._guard()
        return [self._copy_key(row) for row in self.keys.values()]

    async def add_key(
        self,
        key_hash: str,
        key_id: str,
        rpm: int | None = None,
        tpm: int | None = None,
        models: list[str] | None = None,
    ) -> bool:
        self._guard()
        if key_hash in self.keys:
            return False
        self.keys[key_hash] = {
            "key_hash": key_hash,
            "key_id": key_id,
            "rpm": rpm,
            "tpm": tpm,
            "models": list(models) if models is not None else None,
            "enabled": True,
        }
        return True

    async def rotate_key(self, old_hash: str, new_hash: str) -> dict[str, Any]:
        self._guard()
        if old_hash == new_hash:
            row = self.keys.get(old_hash)
            if row is None:
                raise StorageNotFound("key not found", status_code=404)
            return {
                "rotated": True,
                "already_rotated": False,
                "key_id": row["key_id"],
            }
        if old_hash in self.keys:
            if new_hash in self.keys:
                raise StorageConflict(
                    "new key hash already belongs to another key",
                    status_code=409,
                )
            rebuilt: dict[str, dict[str, Any]] = {}
            for key_hash, row in self.keys.items():
                if key_hash != old_hash:
                    rebuilt[key_hash] = row
                    continue
                moved = dict(row)
                moved["key_hash"] = new_hash
                rebuilt[new_hash] = moved
            self.keys = rebuilt
            return {
                "rotated": True,
                "already_rotated": False,
                "key_id": self.keys[new_hash]["key_id"],
            }
        if new_hash in self.keys:
            return {
                "rotated": False,
                "already_rotated": True,
                "key_id": self.keys[new_hash]["key_id"],
            }
        raise StorageNotFound("key not found", status_code=404)

    async def set_key_enabled(self, key_hash: str, enabled: bool) -> bool:
        self._guard()
        row = self.keys.get(key_hash)
        if row is None:
            raise StorageNotFound("key not found", status_code=404)
        row["enabled"] = bool(enabled)
        return True

    async def delete_key(self, key_hash: str) -> bool:
        self._guard()
        if key_hash not in self.keys:
            raise StorageNotFound("key not found", status_code=404)
        del self.keys[key_hash]
        return True

    # --- affinity domain --------------------------------------------------

    async def load_affinity(self) -> list[dict[str, Any]]:
        self._guard()
        rows = sorted(self.affinity.values(), key=lambda row: row["created_at"])
        return [dict(row) for row in rows]

    async def upsert_affinity(
        self, rows: Iterable[Mapping[str, Any] | Sequence[Any]]
    ) -> dict[str, Any]:
        self._guard()
        written = 0
        unbound = 0
        for item in rows:
            binding = self._normalise_binding(item)
            proxy = binding["proxy"]
            if proxy is not None and proxy not in self.proxies:
                proxy = None
                unbound += 1
            existing = self.affinity.get(binding["session_key"])
            if existing is None:
                self.affinity[binding["session_key"]] = {
                    "session_key": binding["session_key"],
                    "proxy": proxy,
                    # A later move must never rewrite the creation time.
                    "created_at": binding["created_at"],
                    "last_used_at": binding["last_used_at"],
                }
            else:
                existing["proxy"] = proxy
                existing["last_used_at"] = binding["last_used_at"]
            written += 1
        return {"written": written, "unbound": unbound}

    async def clear_affinity(self, session_keys: Iterable[str]) -> int:
        self._guard()
        cleared = 0
        for key in dict.fromkeys(key for key in session_keys if key):
            row = self.affinity.get(key)
            if row is None:
                continue
            row["proxy"] = None
            cleared += 1
        return cleared

    # --- persona domain ---------------------------------------------------

    async def load_personas(self) -> list[dict[str, Any]]:
        self._guard()
        rows = sorted(self.personas.values(), key=lambda row: row["created_at"])
        return [dict(row) for row in rows]

    async def get_persona(self, quota_key: str) -> dict[str, Any] | None:
        self._guard()
        row = self.personas.get(quota_key)
        return None if row is None else dict(row)

    async def upsert_persona(self, persona: Any) -> bool:
        self._guard()
        row = self._normalise_persona(persona)
        existing = self.personas.get(row["quota_key"])
        if existing is not None and existing["generation"] > row["generation"]:
            return False
        self.personas[row["quota_key"]] = row
        return True

    # --- request log / stats ---------------------------------------------

    async def log_request(self, entry: Any) -> None:
        self._guard()
        self.log.append(self._normalise_log(entry))

    async def recent_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        self._guard()
        if (isinstance(limit, bool) or not isinstance(limit, int)
                or not RECENT_LIMIT_MIN <= limit <= RECENT_LIMIT_MAX):
            raise StorageAPIError(
                422, "recent limit must be between 1 and 1000"
            )
        rows = sorted(self.log, key=lambda row: row["ts"], reverse=True)
        return [dict(row) for row in rows[:limit]]

    async def stats_summary(self, since: float | None = None) -> dict[str, Any]:
        self._guard()
        groups: dict[str, dict[str, float]] = {}
        total = {"requests": 0, "errors": 0, "tokens": 0, "latency_sum": 0.0}
        for entry in self.log:
            if since is not None and entry["ts"] < since:
                continue
            group = groups.setdefault(
                entry["key_id"],
                {"requests": 0, "errors": 0, "tokens": 0, "latency_sum": 0.0},
            )
            for target in (group, total):
                target["requests"] += 1
                target["errors"] += _is_error(entry)
                target["tokens"] += _tokens(entry)
                target["latency_sum"] += float(entry.get("latency_ms") or 0.0)
        out: dict[str, Any] = {}
        for key_id, group in groups.items():
            requests = int(group["requests"])
            out[key_id] = {
                "requests": requests,
                "errors": int(group["errors"]),
                "tokens": int(group["tokens"]),
                "avg_latency_ms": round(group["latency_sum"] / requests, 1),
            }
        requests = int(total["requests"])
        out["total"] = {
            "requests": requests,
            "errors": int(total["errors"]),
            "tokens": int(total["tokens"]),
            "avg_latency_ms": (
                round(total["latency_sum"] / requests, 1) if requests else 0.0
            ),
        }
        return out

    async def stats_by_account(
        self, since: float | None = None
    ) -> dict[str, dict[str, int]]:
        self._guard()
        groups: dict[str, dict[str, int]] = {}
        for entry in self.log:
            if since is not None and entry["ts"] < since:
                continue
            account = entry.get("account")
            if account is None:
                continue
            group = groups.setdefault(
                str(account), {"requests": 0, "errors": 0, "tokens": 0}
            )
            group["requests"] += 1
            group["errors"] += _is_error(entry)
            group["tokens"] += _tokens(entry)
        return groups

    async def stats_daily(self, since: float = 0.0) -> list[dict[str, Any]]:
        self._guard()
        cutoff = 0.0 if since is None else float(since)
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in self.log:
            if entry["ts"] < cutoff:
                continue
            account = entry.get("account")
            if account is None:
                continue
            key = (_utc_day(entry["ts"]), str(account))
            group = groups.setdefault(
                key,
                {"day": key[0], "account": key[1],
                 "requests": 0, "errors": 0, "tokens": 0},
            )
            group["requests"] += 1
            group["errors"] += _is_error(entry)
            group["tokens"] += _tokens(entry)
        return [groups[key] for key in sorted(groups)]

    # --- seeding / normalisation -----------------------------------------

    def _seed_proxy(self, item: str | Mapping[str, Any]) -> None:
        overrides = {} if isinstance(item, str) else dict(item)
        url = item if isinstance(item, str) else overrides.pop("proxy")
        row = {
            "proxy": url,
            "enabled": True,
            "added_at": time.time(),
            "quota_until": 0.0,
            "banned_until": 0.0,
            "permanently_banned": False,
            "listed": True,
        }
        row.update(overrides)
        row["enabled"] = bool(row["enabled"])
        row["added_at"] = float(row["added_at"])
        row["quota_until"] = float(row["quota_until"])
        row["banned_until"] = float(row["banned_until"])
        row["permanently_banned"] = bool(row["permanently_banned"])
        row["listed"] = bool(row["listed"])
        self.proxies[url] = row

    def _seed_key(self, item: Mapping[str, Any]) -> None:
        row = self._copy_key(item)
        self.keys[row["key_hash"]] = row

    @staticmethod
    def _copy_key(item: Mapping[str, Any]) -> dict[str, Any]:
        models = item.get("models")
        return {
            "key_hash": item["key_hash"],
            "key_id": item.get("key_id", ""),
            "rpm": item.get("rpm"),
            "tpm": item.get("tpm"),
            "models": list(models) if isinstance(models, list) else None,
            "enabled": bool(item.get("enabled", True)),
        }

    def _seed_affinity(
        self, item: Mapping[str, Any] | Sequence[Any]
    ) -> None:
        binding = self._normalise_binding(item)
        if binding["proxy"] is not None and binding["proxy"] not in self.proxies:
            binding["proxy"] = None  # FK ON DELETE SET NULL equivalent
        self.affinity[binding["session_key"]] = binding

    @staticmethod
    def _normalise_binding(
        item: Mapping[str, Any] | Sequence[Any]
    ) -> dict[str, Any]:
        if isinstance(item, Mapping):
            session_key = item["session_key"]
            proxy = item.get("proxy")
            created_at = item["created_at"]
            last_used_at = item["last_used_at"]
        else:
            session_key, proxy, created_at, last_used_at = item
        return {
            "session_key": session_key,
            "proxy": proxy,
            "created_at": float(created_at),
            "last_used_at": float(last_used_at),
        }

    def _seed_persona(self, item: Mapping[str, Any] | Any) -> None:
        row = self._normalise_persona(item)
        self.personas[row["quota_key"]] = row

    @staticmethod
    def _normalise_persona(item: Mapping[str, Any] | Any) -> dict[str, Any]:
        source = item if isinstance(item, Mapping) else {
            name: getattr(item, name) for name in _PERSONA_FIELDS
        }
        return {
            "quota_key": source["quota_key"],
            "session_id": source["session_id"],
            "project_id": source["project_id"],
            "trace_id": source["trace_id"],
            "created_at": float(source["created_at"]),
            "generation": int(source["generation"]),
        }

    def _seed_log(self, item: Mapping[str, Any] | Any) -> None:
        self.log.append(self._normalise_log(item))

    @staticmethod
    def _normalise_log(entry: Any) -> dict[str, Any]:
        if is_dataclass(entry) and not isinstance(entry, type):
            data: Mapping[str, Any] = asdict(entry)
        elif isinstance(entry, Mapping):
            data = entry
        else:  # pragma: no cover - defensive
            raise TypeError("log entry must be a dataclass or a mapping")
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

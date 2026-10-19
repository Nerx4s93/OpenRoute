"""Proxy pool manager: round-robin, circuit breaker, 429 cooldown/parking.

This is the direct analogue of ``NVRoute/app/accounts.py`` — the key difference
is that a pool member IS an HTTP(S) proxy (there is no separate token)::

    Proxy (one ``http://user:pass@host:port``) -> one opencode.ai quota bucket

CONTRACT (stable signatures used by ``main.py``):

- ``next_proxy(exclude=None) -> Proxy`` — a ready proxy (in pool, enabled,
  not manually disabled, not cooling, not on the local RPM cap), uniform
  round-robin over the STABLE ``self.proxies`` list; raises
  ``NoReadyProxyError(retry_after)`` when none is ready.
- ``record_request(proxy)`` — burn one slot of the proxy's rolling RPM window;
  call right before sending upstream. Does NOT touch ``proxy.requests``.
- ``mark_success(proxy)`` / ``mark_failure(proxy, reason, retry_after=None)``
  / ``status() -> dict``.
- ``refresh_proxies()`` — fetch from ``PROXIES_URL``, upsert into ``ProxyDB``
  (when bound), health-probe, rebuild the runtime pool. Returns ``True`` on a
  successful fetch, ``False`` on fetch error (caller retries sooner).

Upstream probe target: ``GET {opencode_models_url}`` (the public catalogue,
which does not count against the chat quota).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import httpx

from .config import PROVIDER_NAME, Settings

logger = logging.getLogger(__name__)

#: ``scheme://user:pass@`` -> ``scheme://***@`` inside arbitrary text.
_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s@]*@")


def redact_url(value: str | None, secret: str | None = None) -> str:
    """Strip credentials from a URL (or any text containing one) for logs/status.

    Removes ``user:pass@`` userinfo (proxy and DSN credentials) and, when given,
    every occurrence of ``secret``. Used so ``PROXIES_URL``/``POSTGRES_DSN`` can
    never leak through ``last_proxies_fetch`` (``/health``, ``/admin/status``)
    or the logs.
    """
    out = value or ""
    if secret:
        out = out.replace(secret, "***")
    return _USERINFO_RE.sub(r"\1***@", out)


#: Rolling window for the local RPM cap (mirrors NVRoute RPM_WINDOW_SECONDS).
RPM_WINDOW_SECONDS: float = 60.0

#: Default 429 cooldown when the upstream sent no usable Retry-After.
#: Mirrors NVRoute's RPM_WINDOW_SECONDS = 60.
DEFAULT_COOLDOWN_SECONDS: float = 60.0


def mask_proxy(proxy: str | None) -> str:
    """Return ``host:port`` (credentials and scheme stripped) for logs/status.

    Empty/None -> ``"***"``; never leaks proxy ``user:pass``.
    """
    if not proxy:
        return "***"
    try:
        u = httpx.URL(proxy)
        if u.host and u.port is not None:
            return f"{u.host}:{u.port}"
    except Exception:
        pass
    return proxy.split("@")[-1]


def _masked_url(proxy: str | None) -> str | None:
    """``scheme://host:port`` with credentials removed (or ``None``)."""
    if not proxy:
        return None
    try:
        u = httpx.URL(proxy)
        if u.host and u.port is not None:
            return f"{u.scheme}://{u.host}:{u.port}"
    except Exception:
        pass
    return proxy.split("@")[-1]


class NoReadyProxyError(RuntimeError):
    """No proxy is ready (see ``ProxyManager.next_proxy``).

    CONTRACT (main.py): ``retry_after`` — seconds until the earliest proxy
    becomes ready (end of cooldown OR a slot in the RPM window), so the
    gateway can answer 429/503 with ``Retry-After``; ``None`` means waiting is
    pointless (pool permanently empty / all candidates already excluded by the
    current request's retry loop).
    """

    def __init__(self, retry_after: Optional[float] = None) -> None:
        self.retry_after = retry_after
        if retry_after is None:
            super().__init__("no ready proxy (pool empty or all candidates excluded)")
        else:
            super().__init__(f"no ready proxy (retry_after={retry_after:.1f}s)")


@dataclass
class Proxy:
    """One pool member: an HTTP(S) proxy URL ``http://user:pass@host:port``."""

    url: str
    client: Optional[httpx.AsyncClient] = None
    requests: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    cooldown_until: float = 0.0  # time.monotonic(); 0 = not cooling
    manually_disabled: bool = False  # disabled via admin UI (lives in ProxyDB)
    disabled: bool = False  # reserved; proxies are never permanently dead
    last_error: Optional[str] = None
    # --- internal breaker state ---
    _fails: deque = field(default_factory=deque)  # timestamps of recent failures
    _cooldown_n: int = 0  # how many times this proxy went into cooldown
    _client_proxy: Optional[str] = None  # proxy the current client was built for
    _rpm: deque = field(default_factory=deque)  # timestamps in the rolling RPM window
    _throttle_n: int = 0  # consecutive 429 cooldowns without success

    @property
    def enabled(self) -> bool:
        """In pool and not manually disabled."""
        return not self.manually_disabled


class ProxyManager:
    """Round-robin proxy pool with circuit breaker and 429 parking.

    INVARIANT (single event loop): synchronous mutations of breaker state
    (``mark_failure`` / ``mark_success`` / ``next_proxy`` / ``record_request``)
    contain no ``await`` between read and write and therefore take no lock.
    The lock only guards ``refresh_proxies`` (which awaits probes): after every
    await the proxy state is re-checked — pre-probe snapshots cannot be
    trusted. Multi-worker (a split brain) would need external shared state
    (Redis etc.) — out of scope for this version.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self.proxies: list[Proxy] = []
        self.last_fetch_status: Optional[str] = None
        self.db = None  # optional ProxyDB (duck-typed); bound via bind_db()
        self._lock = asyncio.Lock()
        self._fetcher = httpx.AsyncClient(timeout=15.0)
        self._rr = 0  # index into the STABLE self.proxies list
        self._closed = False
        self._bg_tasks: set[asyncio.Task] = set()  # strong refs to fire-and-forget tasks

    def bind_db(self, db) -> None:
        """Attach a ``ProxyDB`` so ``refresh_proxies`` can upsert members."""
        self.db = db

    # --- pool membership (source of truth: ProxyDB) ---

    def apply_proxies(self, rows: list[tuple[str, bool]]) -> None:
        """Sync the runtime pool with ``ProxyDB.all()`` ``[(proxy, enabled)]``.

        Existing objects are preserved (runtime counters/breaker survive);
        rows removed from the DB are dropped and their clients closed.
        """
        by_url = {p.url: p for p in self.proxies}
        new: list[Proxy] = []
        for url, enabled in rows:
            p = by_url.pop(url, None) or Proxy(url)
            p.manually_disabled = not enabled
            new.append(p)
        for gone in by_url.values():
            client, gone.client, gone._client_proxy = gone.client, None, None
            if client is not None:
                self._close_client_soon(client, f"apply_proxies drop {mask_proxy(gone.url)}")
        self.proxies = new
        self._rr = self._rr % len(new) if new else 0

    def remove_proxy(self, proxy: str) -> bool:
        """Remove from the pool; client is closed in the background."""
        for i in range(len(self.proxies)):
            if self.proxies[i].url == proxy:
                p = self.proxies.pop(i)
                client, p.client, p._client_proxy = p.client, None, None
                if client is not None:
                    self._close_client_soon(client, f"remove_proxy {mask_proxy(proxy)}")
                n = len(self.proxies)
                self._rr = self._rr % n if n else 0
                return True
        return False

    def set_manually_disabled(self, proxy: str, disabled: bool) -> bool:
        """Manual enable/disable from the admin UI (persisted in ProxyDB)."""
        for p in self.proxies:
            if p.url == proxy:
                p.manually_disabled = disabled
                return True
        return False

    # --- refresh ---

    @staticmethod
    def _proxy_url(p: object) -> Optional[str]:
        """Proxy-list item -> URL; ``None`` = malformed item (skip it).

        Accepts ``dict {host, port[, user, password]}`` and strings
        ``"http://user:pass@host:port"`` / ``"host:port"``. One bad element
        must not sink the whole fetch — each item is validated separately.
        """
        if isinstance(p, str):
            s = p.strip()
            if not s or any(c.isspace() for c in s):
                return None
            if "://" not in s:
                s = "http://" + s
            try:
                u = httpx.URL(s)
            except Exception:
                return None
            if u.scheme not in ("http", "https") or not u.host or u.port is None:
                return None
            return str(u)
        if isinstance(p, dict):
            host, port = p.get("host"), p.get("port")
            if not isinstance(host, str) or not host.strip():
                return None
            if not isinstance(port, (int, str)) or isinstance(port, bool):
                return None
            try:
                port_i = int(port)
            except (TypeError, ValueError):
                return None
            if not (0 < port_i < 65536):
                return None
            user, password = p.get("user"), p.get("password")
            cred = ""
            if user:
                cred = f"{quote(str(user), safe='')}:{quote(str(password or ''), safe='')}@"
            return f"http://{cred}{host.strip()}:{port_i}"
        return None

    @staticmethod
    def _describe_proxy_item(p: object) -> str:
        """Description of a malformed item for logs WITHOUT values (no creds)."""
        if isinstance(p, dict):
            return f"dict, keys={sorted(p.keys())}"
        return type(p).__name__

    @staticmethod
    def _proxy_hostport(proxy_url: str) -> str:
        """host:port from a proxy URL (credentials stripped — for logs)."""
        return proxy_url.split("@")[-1]

    async def refresh_proxies(self) -> bool:
        """Fetch the proxy list, upsert/probe, rebuild the pool.

        Malformed individual items are skipped with a warning; duplicates are
        collapsed. A malformed top-level payload is a fetch error (``False`` ->
        caller retries soon). ``True`` = fetch succeeded.
        """
        try:
            resp = await self._fetcher.get(self._s.proxies_url)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                items = data.get("proxies")
            elif isinstance(data, list):
                items = data
            else:
                raise ValueError(f"unexpected proxies payload type {type(data).__name__}")
            if items is None:
                items = []
            if not isinstance(items, (list, tuple)):
                raise ValueError(f"unexpected 'proxies' value type {type(items).__name__}")
        except Exception as exc:
            reason = redact_url(str(exc))
            self.last_fetch_status = f"error: {reason}"
            logger.error("Failed to fetch proxies from %s: %s",
                         redact_url(self._s.proxies_url), reason)
            return False

        urls: list[str] = []
        for p in (items or []):
            u = self._proxy_url(p)
            if u is None:
                logger.warning("Skipping malformed proxy item (%s)", self._describe_proxy_item(p))
            else:
                urls.append(u)
        urls = list(dict.fromkeys(urls))  # dedupe by URL

        # Persist membership first (source of truth), then rebuild the pool.
        if self.db is not None:
            try:
                await asyncio.to_thread(self.db.upsert_many, urls)
                rows = await asyncio.to_thread(self.db.all)
            except Exception as exc:
                logger.error("ProxyDB upsert/all failed: %s", exc)
                rows = [(u, True) for u in urls]
        else:
            rows = [(u, True) for u in urls]

        async with self._lock:
            self.apply_proxies(rows)
            snapshot = list(self.proxies)

        # Probe live members concurrently (outside the lock — probes await).
        probe_urls = [p.url for p in snapshot if p.enabled]
        verdicts = await asyncio.gather(*(self._probe_proxy(u) for u in probe_urls))
        alive = {u for u, ok in zip(probe_urls, verdicts) if ok}

        # Apply verdicts under the lock, re-checking state after each await.
        async with self._lock:
            now = time.monotonic()
            for p in self.proxies:
                if not p.enabled:
                    continue
                if p.url in alive:
                    self.mark_success(p)  # proxy came back -> lift cooldown early
                else:
                    if p.cooldown_until <= now:
                        p.cooldown_until = now + self._s.account_cooldown_seconds
                    p.last_error = "health-check: dead"
            enabled = sum(1 for p in self.proxies if p.enabled and not p.disabled)
            ready = sum(
                1 for p in self.proxies
                if p.enabled and not p.disabled and p.cooldown_until <= now
                and not self._rpm_capped(p, now)
            )
            self.last_fetch_status = (
                f"ok at {time.strftime('%Y-%m-%d %H:%M:%S')}: "
                f"{len(urls)} proxies ({len(alive)} alive), "
                f"{enabled}/{len(self.proxies)} enabled, {ready} ready"
            )

        if self.proxies and ready == 0:
            await self._alert(
                f"{PROVIDER_NAME}: 0 ready proxies after refresh_proxies "
                f"({enabled}/{len(self.proxies)} enabled, {len(alive)} alive)"
            )
        return True

    async def _probe_proxy(self, proxy_url: str) -> bool:
        """Proxy alive iff ``GET {opencode_models_url}`` returns status < 500
        and != 407 (Proxy Authentication Required). Any transport error = dead.
        """
        hostport = self._proxy_hostport(proxy_url)
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=httpx.Timeout(self._s.health_check_timeout, connect=5.0),
            ) as client:
                resp = await client.get(self._s.opencode_models_url)
        except Exception as exc:
            logger.info("Proxy %s dead: %s", hostport, type(exc).__name__)
            return False
        if resp.status_code >= 500 or resp.status_code == 407:
            logger.info("Proxy %s dead: HTTP %d", hostport, resp.status_code)
            return False
        return True

    # --- upstream clients ---

    def ensure_client(self, proxy: Proxy) -> httpx.AsyncClient:
        """Lazily build/cache the ``httpx.AsyncClient`` bound to ``proxy.url``.

        After ``aclose()`` the manager never resurrects clients: it raises
        ``RuntimeError`` (otherwise a new client would silently appear for a
        "closed" manager).
        """
        if self._closed:
            raise RuntimeError("ensure_client() after aclose(): manager is closed")
        if proxy.client is not None and proxy._client_proxy == proxy.url:
            return proxy.client
        old = proxy.client
        proxy.client = httpx.AsyncClient(
            base_url=self._s.opencode_base_url.rstrip("/"),
            proxy=proxy.url,
            timeout=httpx.Timeout(self._s.upstream_timeout, connect=self._s.connect_timeout),
        )
        proxy._client_proxy = proxy.url
        if old is not None:
            self._close_client_soon(old, f"client rebuilt for {mask_proxy(proxy.url)}")
        return proxy.client

    def _close_client_soon(self, client: httpx.AsyncClient, why: str) -> None:
        """Close an httpx client "in the background", holding a STRONG ref to
        the task (``create_task`` returns a weakref-compatible Future: without a
        strong ref the task can be GC'd before it completes). With a running
        event loop -> task in ``self._bg_tasks``; without one (sync context)
        -> close directly via ``asyncio.run``.
        """
        async def _close() -> None:
            try:
                await client.aclose()
            except Exception:
                logger.debug("Failed to close client (%s)", why, exc_info=True)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(_close())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        else:
            try:
                asyncio.run(_close())
            except Exception:
                logger.debug("Direct client close failed (%s)", why, exc_info=True)

    # --- local RPM cap ---

    def record_request(self, proxy: Proxy) -> None:
        """CONTRACT: main.py calls this right before sending upstream.

        Burns one slot in the proxy's rolling 60-second window. Does NOT touch
        ``proxy.requests`` — the total counter is incremented by main.py.
        """
        now = time.monotonic()
        proxy._rpm.append(now)
        self._prune_rpm(proxy, now)

    def _prune_rpm(self, proxy: Proxy, now: float) -> None:
        while proxy._rpm and proxy._rpm[0] <= now - RPM_WINDOW_SECONDS:
            proxy._rpm.popleft()

    def _rpm_capped(self, proxy: Proxy, now: float) -> bool:
        if self._s.openroute_rpm_limit <= 0:  # 0/negative = cap disabled
            return False
        self._prune_rpm(proxy, now)
        return len(proxy._rpm) >= self._s.openroute_rpm_limit

    def _rpm_wait(self, proxy: Proxy, now: float) -> float:
        """Seconds until an RPM-window slot frees up (0 = not capped)."""
        if self._s.openroute_rpm_limit <= 0:
            return 0.0
        self._prune_rpm(proxy, now)
        if len(proxy._rpm) < self._s.openroute_rpm_limit:
            return 0.0
        return max(0.0, proxy._rpm[0] + RPM_WINDOW_SECONDS - now)

    # --- balancing ---

    def next_proxy(self, exclude: Optional[set[str]] = None) -> Proxy:
        """Uniform round-robin over ready proxies; never returns ``None``.

        Ready = enabled (in pool, not manually disabled), not disabled, not in
        cooldown, not on the RPM cap, not in ``exclude``. The ``_rr`` index
        lives over the STABLE ``self.proxies`` list (not over the recomputed
        ready subset): each pick walks forward from ``_rr``, skipping non-ready
        entries, and leaves ``_rr`` right after the chosen entry — so a proxy is
        never picked twice in a row and never skipped when a neighbour cools
        down.

        CONTRACT: none ready -> ``NoReadyProxyError(retry_after)``:
        ``retry_after`` = seconds until the earliest candidate frees up
        (cooldown / RPM slot) or ``None`` when the pool is permanently empty /
        all candidates are excluded by the request's retry loop.
        """
        exclude = exclude or set()
        now = time.monotonic()
        n = len(self.proxies)
        if n == 0:
            raise NoReadyProxyError(None)
        for i in range(n):
            idx = (self._rr + i) % n
            p = self.proxies[idx]
            if (
                p.enabled and not p.disabled and not p.manually_disabled
                and p.url not in exclude
                and p.cooldown_until <= now and not self._rpm_capped(p, now)
            ):
                self._rr = (idx + 1) % n
                return p
        raise NoReadyProxyError(self._earliest_ready_seconds(exclude, now))

    def _earliest_ready_seconds(self, exclude: set[str], now: float) -> Optional[float]:
        """``retry_after``: seconds until the earliest candidate becomes ready.

        Only enabled, non-disabled proxies NOT in ``exclude`` are considered:
        candidates excluded by the retry loop will not "revive" by waiting.
        ``None`` = nothing to wait for (no candidates at all).
        """
        best: Optional[float] = None
        for p in self.proxies:
            if not p.enabled or p.disabled or p.manually_disabled or p.url in exclude:
                continue
            wait = max(p.cooldown_until - now, self._rpm_wait(p, now), 0.0)
            if best is None or wait < best:
                best = wait
        return best

    # --- breaker ---

    def mark_failure(self, proxy: Proxy, reason: str,
                     retry_after: Optional[float] = None) -> None:
        """Record a failure. Two modes:

        429 throttling (``retry_after`` given, or ``"429"`` in ``reason``):
        cooldown for the upstream's Retry-After, or ``PROXY_COOLDOWN_DEFAULT``
        (60 s) when absent — BYPASSING the threshold logic (the proxy is already
        overheated; counting errors is pointless). ``account_throttle_threshold``
        CONSECUTIVE such 429 cooldowns (the counter resets only on success) ->
        PARK for ``account_throttle_cooldown_seconds`` (1 h). There is no
        "release the proxy" step: the member IS the proxy. No sleep — the caller
        retries the next proxy immediately.

        Network/5xx: rolling window; at ``account_fail_threshold`` within
        ``account_fail_window_seconds`` -> cooldown with exponential growth
        (``account_cooldown_seconds`` * 2**(n-1), capped by
        ``account_cooldown_max_seconds``); the caller backs off.
        """
        proxy.errors += 1
        proxy.consecutive_errors += 1
        proxy.last_error = reason
        now = time.monotonic()
        if retry_after is not None or "429" in reason:
            duration = float(retry_after) if retry_after is not None else DEFAULT_COOLDOWN_SECONDS
            if not proxy.disabled:
                proxy._throttle_n += 1
                if proxy._throttle_n >= self._s.account_throttle_threshold:
                    # a larger upstream Retry-After (e.g. 2 h) is not shortened
                    duration = max(duration, self._s.account_throttle_cooldown_seconds)
                    proxy._throttle_n = 0
                    logger.warning(
                        "Proxy %s: %d consecutive 429s -> parking for %.0fs",
                        mask_proxy(proxy.url), self._s.account_throttle_threshold, duration,
                    )
                    self._fire_alert(
                        f"{PROVIDER_NAME}: proxy {mask_proxy(proxy.url)} throttled "
                        f"({self._s.account_throttle_threshold}x 429 in a row) — parking "
                        f"{duration:.0f}s"
                    )
                proxy.cooldown_until = max(proxy.cooldown_until, now + duration)
                proxy._fails.clear()
            logger.warning(
                "Proxy %s: upstream 429 -> cooldown %.0fs: %s",
                mask_proxy(proxy.url), duration, reason,
            )
            return
        proxy._fails.append(now)
        while proxy._fails and proxy._fails[0] <= now - self._s.account_fail_window_seconds:
            proxy._fails.popleft()
        if proxy.disabled or len(proxy._fails) < self._s.account_fail_threshold:
            return
        proxy._cooldown_n += 1
        duration = min(
            self._s.account_cooldown_seconds * (2 ** (proxy._cooldown_n - 1)),
            self._s.account_cooldown_max_seconds,
        )
        fails = len(proxy._fails)
        proxy.cooldown_until = now + duration
        proxy._fails.clear()
        logger.warning(
            "Proxy %s: %d failures in %.0fs -> cooldown %.0fs (recurrence #%d): %s",
            mask_proxy(proxy.url), fails, self._s.account_fail_window_seconds,
            duration, proxy._cooldown_n, reason,
        )

    def mark_success(self, proxy: Proxy) -> None:
        """Success: clears the error counter, window, cooldown, and 429 streak.
        Never clears ``disabled``."""
        if proxy.disabled:
            return
        proxy.consecutive_errors = 0
        proxy._fails.clear()
        proxy._throttle_n = 0
        proxy.cooldown_until = 0.0

    # --- alerts ---

    async def _alert(self, text: str) -> None:
        """POST {"text": ...} to ``alert_webhook_url``; all errors swallowed."""
        if not self._s.alert_webhook_url:
            return
        try:
            await self._fetcher.post(self._s.alert_webhook_url, json={"text": text})
        except Exception:
            logger.debug("Alert webhook failed", exc_info=True)

    def _fire_alert(self, text: str) -> None:
        """Schedule ``_alert`` from a sync context; no running loop -> no-op."""
        try:
            asyncio.get_running_loop().create_task(self._alert(text))
        except RuntimeError:
            pass

    # --- status ---

    def status(self) -> dict:
        """Separate counters (do not mix "dead" with "manually disabled").

        CONTRACT (main.py /health): ``proxies_total`` — every row;
        ``proxies_enabled`` — in rotation (not manually disabled, not dead);
        ``proxies_cooling`` — in cooldown; ``proxies_rpm_capped`` — hit the
        local rolling RPM cap (may overlap with cooling). Per-proxy:
        ``rpm_window`` — current RPM load, ``rpm_capped`` — whether capped.
        """
        now = time.monotonic()
        enabled = [p for p in self.proxies if p.enabled and not p.disabled]
        return {
            "provider": PROVIDER_NAME,
            "proxies_total": len(self.proxies),
            "proxies_enabled": len(enabled),
            "proxies_dead": sum(1 for p in self.proxies if p.disabled),
            "proxies_manually_disabled": sum(1 for p in self.proxies if p.manually_disabled),
            "proxies_cooling": sum(1 for p in enabled if p.cooldown_until > now),
            "proxies_rpm_capped": sum(1 for p in enabled if self._rpm_capped(p, now)),
            # Defensive: never expose userinfo/DSN credentials in the snapshot.
            "last_proxies_fetch": redact_url(self.last_fetch_status) if self.last_fetch_status else None,
            "proxies": [
                {
                    "proxy": mask_proxy(p.url),
                    "url": _masked_url(p.url),
                    "enabled": p.enabled,
                    "requests": p.requests,
                    "errors": p.errors,
                    "consecutive_errors": p.consecutive_errors,
                    "disabled": p.disabled,
                    "manual": p.manually_disabled,
                    "cooldown_seconds_left": (
                        round(p.cooldown_until - now, 1) if p.cooldown_until > now else 0.0
                    ),
                    "rpm_window": len(p._rpm) if self._s.openroute_rpm_limit > 0 else 0,
                    "rpm_capped": self._rpm_capped(p, now),
                    "last_error": p.last_error,
                }
                for p in self.proxies
            ],
        }

    async def aclose(self) -> None:
        """Close the fetcher and all clients; safe to call twice."""
        if self._closed:
            return
        self._closed = True
        for p in self.proxies:
            client, p.client, p._client_proxy = p.client, None, None
            if client is not None:
                await client.aclose()
        await self._fetcher.aclose()

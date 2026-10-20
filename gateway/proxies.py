"""Proxy pool manager: round-robin, circuit breaker, 429 cooldown/parking.

This is the direct analogue of ``NVRoute/app/accounts.py`` — the key difference
is that a pool member IS an HTTP(S) proxy (there is no separate token)::

    Proxy (one ``http://user:pass@host:port``) -> one opencode.ai quota bucket.
    Do not merge buckets by entry IP: an IPv4 entry may exit through an
    unrelated IPv6 address, so only the purchased proxy URL is a safe key.

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
    becomes ready (end of cooldown OR a slot in the RPM window OR the end of a
    group's ``quota_until``/``banned_until`` parking), so the gateway can answer
    429/503 with ``Retry-After``; ``None`` means waiting is pointless (including
    a permanently banned abusive-traffic bucket).

    ``kind`` (additive, keeps the old constructor/``retry_after`` contract):

    - ``"empty"`` — the pool has no usable proxies at all (503),
    - ``"excluded"`` — every usable proxy was already tried by this request
      (502),
    - ``"saturated"`` — proxies exist but are cooling/RPM-capped/parked; wait
      ``retry_after`` seconds (429).
    """

    def __init__(self, retry_after: Optional[float] = None,
                 kind: str = "empty") -> None:
        self.retry_after = retry_after
        self.kind = kind
        if retry_after is None:
            if kind == "excluded":
                msg = "no ready proxy (all candidates already excluded)"
            else:
                msg = "no ready proxy (pool empty or all disabled)"
        else:
            msg = f"no ready proxy ({kind}, retry_after={retry_after:.1f}s)"
        super().__init__(msg)


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


def canonical_quota_key(proxy_or_url: object) -> str:
    """Stable per-proxy quota-bucket key.

    The upstream daily quota is an exit-IP property, but the exit IP is NOT
    the purchased proxy host: an IPv4 entry can exit through a different IPv6
    address, and different ports on one entry IPv4 can have unrelated IPv6
    exits. Grouping by the entry host therefore parks healthy proxies.

    The only safe local key is the purchased endpoint itself: ``host:port``
    with credentials stripped, lowercased and IPv6 bracketed. Empty or
    unparseable input yields ``""``.
    """
    raw = getattr(proxy_or_url, "url", proxy_or_url)
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        u = httpx.URL(text)
        if u.host and u.port is not None:
            host = u.host.lower()
            if ":" in host:
                host = f"[{host}]"
            return f"{host}:{u.port}"
    except Exception:
        pass
    tail = text.split("@")[-1]
    if "://" in tail:
        tail = tail.split("://", 1)[1]
    return tail.lower().rstrip("/")


@dataclass
class EgressGroup:
    """One quota bucket: one purchased proxy endpoint.

    Upstream quota/abusive bans are keyed by exit IP, but the exit IP cannot be
    derived from the entry URL (IPv4 entry may use IPv6 egress). We therefore
    keep one bucket per proxy endpoint and never merge by the entry host.
    ``host`` holds the non-secret ``host:port`` key.

    ``quota_until`` / ``banned_until`` are ``time.time()`` (wall-clock) epochs:
    a daily upstream quota reset is tied to the UTC calendar and the values are
    shown to operators. ``permanently_banned`` is the abusive-traffic verdict:
    it never expires and the bucket must never be selected or probed again.
    Per-URL breaker state (``Proxy.cooldown_until``, ``_rpm``) stays on the
    ``time.monotonic()`` clock.
    """

    host: str
    proxies: list[Proxy] = field(default_factory=list, repr=False)
    quota_until: float = 0.0
    banned_until: float = 0.0
    permanently_banned: bool = False
    _rr: int = field(default=0, repr=False)

    def parked_until(self) -> float:
        """Current timed parking epoch (``max(quota, ban)``; 0 = not parked)."""
        return max(self.quota_until, self.banned_until)

    def is_parked(self, now_wall: float) -> bool:
        """Permanent abusive-traffic ban or a timed quota/ban still running."""
        return self.permanently_banned or self.parked_until() > now_wall


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
        self._groups: list[EgressGroup] = []
        self._group_by_key: dict[str, EgressGroup] = {}
        self._group_by_url: dict[str, EgressGroup] = {}
        self.last_fetch_status: Optional[str] = None
        self.db = None  # optional ProxyDB (duck-typed); bound via bind_db()
        self._lock = asyncio.Lock()
        # Serializes whole refreshes (fetch + probe + apply), so the periodic
        # loop and a manual "refresh" request cannot overwrite each other.
        self._refresh_lock = asyncio.Lock()
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
        self._rebuild_groups()

    def load_parking(self, rows: list[tuple[str, float, float, bool]]) -> None:
        """Restore persisted ``(proxy, quota_until, banned_until, permanent)``.

        Called once at startup after :meth:`apply_proxies`. Rows are grouped by
        the canonical quota key; the maximum epoch wins and a permanent ban
        ORs together, matching the runtime invariant that parking may only move
        forward and never expires for abusive traffic.
        """
        by_key: dict[str, tuple[float, float, bool]] = {}
        for proxy, quota_until, banned_until, permanently_banned in rows:
            key = canonical_quota_key(proxy)
            if not key:
                continue
            old_q, old_b, old_perm = by_key.get(key, (0.0, 0.0, False))
            by_key[key] = (
                max(old_q, float(quota_until or 0.0)),
                max(old_b, float(banned_until or 0.0)),
                old_perm or bool(permanently_banned),
            )
        for group in self._groups:
            quota_until, banned_until, permanently_banned = by_key.get(
                group.host, (0.0, 0.0, False))
            group.quota_until = max(group.quota_until, quota_until)
            group.banned_until = max(group.banned_until, banned_until)
            group.permanently_banned = (
                group.permanently_banned or permanently_banned)

    def _rebuild_groups(self) -> None:
        """Rebuild quota buckets from ``self.proxies``, preserving parking.

        A refresh must not lose a ``quota_until``/``banned_until`` or permanent
        abusive-traffic ban set between two fetches: the state is carried over
        by canonical proxy key, not by URL object. First-appearance order
        follows the stable ``self.proxies`` order. Runtime ``Proxy`` objects
        (and therefore cooldown/RPM state) are already preserved by
        :meth:`apply_proxies`.
        """
        previous = {g.host: g for g in self._groups}
        groups: list[EgressGroup] = []
        by_key: dict[str, EgressGroup] = {}
        by_url: dict[str, EgressGroup] = {}
        for proxy in self.proxies:
            key = canonical_quota_key(proxy)
            group = by_key.get(key)
            if group is None:
                old = previous.get(key)
                group = EgressGroup(
                    host=key,
                    quota_until=old.quota_until if old else 0.0,
                    banned_until=old.banned_until if old else 0.0,
                    permanently_banned=old.permanently_banned if old else False,
                    _rr=old._rr if old else 0,
                )
                by_key[key] = group
                groups.append(group)
            group.proxies.append(proxy)
            by_url[proxy.url] = group
        for group in groups:
            group._rr = group._rr % len(group.proxies) if group.proxies else 0
        self._groups = groups
        self._group_by_key = by_key
        self._group_by_url = by_url

    def egress_groups(self) -> list[EgressGroup]:
        """Egress groups in stable (first-appearance) order."""
        return list(self._groups)

    def group_by_key(self, key: str) -> Optional[EgressGroup]:
        """Group by canonical quota key (see :func:`canonical_quota_key`)."""
        return self._group_by_key.get((key or "").lower())

    def group_of(self, proxy_or_url: object) -> Optional[EgressGroup]:
        """Egress group of a :class:`Proxy` or URL; ``None`` if not in pool.

        Falls back to the canonical key so a URL that was just removed still
        resolves to its quota bucket during the same refresh cycle.
        """
        raw = getattr(proxy_or_url, "url", proxy_or_url)
        group = self._group_by_url.get(str(raw))
        if group is not None:
            return group
        return self._group_by_key.get(canonical_quota_key(raw))

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

    async def refresh_proxies(self, bust_cache: bool = False) -> bool:
        """Refresh the pool, optionally forcing the source to bypass its cache.

        ``bust_cache=True`` adds ``?refresh=true`` to ``PROXIES_URL`` (the
        px6-service contract) so a manual admin refresh sees the current
        px6.net list instead of the service's TTL-cached copy. The periodic
        loop keeps using the cached path.
        """
        async with self._refresh_lock:
            return await self._refresh_proxies_locked(bust_cache)

    async def _refresh_proxies_locked(self, bust_cache: bool) -> bool:
        """Fetch the proxy list, upsert/probe, rebuild the pool.

        Malformed individual items are skipped with a warning; duplicates are
        collapsed. A malformed top-level payload is a fetch error (``False`` ->
        caller retries soon). ``True`` = fetch succeeded.
        """
        try:
            url = self._s.proxies_url
            if bust_cache:
                url = str(httpx.URL(url).copy_merge_params({"refresh": "true"}))
            resp = await self._fetcher.get(url)
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
        # A parked group (daily quota / abusive ban) is not probed at all: a
        # successful probe must never lift parking, and hitting the upstream
        # from a quota-exhausted IP is pointless.
        now_wall = time.time()
        parked_urls = {
            p.url
            for g in self.egress_groups()
            if g.is_parked(now_wall)
            for p in g.proxies
        }
        probe_urls = [p.url for p in snapshot if p.enabled and p.url not in parked_urls]
        verdicts = await asyncio.gather(*(self._probe_proxy(u) for u in probe_urls))
        alive = {u for u, ok in zip(probe_urls, verdicts) if ok}

        # Apply verdicts under the lock, re-checking state after each await.
        async with self._lock:
            now = time.monotonic()
            now_wall = time.time()
            for p in self.proxies:
                if not p.enabled:
                    continue
                group = self.group_of(p)
                if group is not None and group.is_parked(now_wall):
                    continue  # never clear quota/banned parking via a probe
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
                and (self.group_of(p) is None
                     or not self.group_of(p).is_parked(now_wall))
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

    @staticmethod
    def _eligible(p: Proxy) -> bool:
        """In rotation by configuration (pool membership / manual flags)."""
        return p.enabled and not p.disabled and not p.manually_disabled

    def _proxy_ready(self, p: Proxy, now_mono: float) -> bool:
        """Enabled and not cooling / RPM-capped (group parking checked by caller)."""
        return (
            self._eligible(p)
            and p.cooldown_until <= now_mono
            and not self._rpm_capped(p, now_mono)
        )

    def _group_parked(self, group: EgressGroup, now_wall: float) -> bool:
        return group.is_parked(now_wall)

    def group_is_ready(self, group: EgressGroup, *,
                       now_mono: Optional[float] = None,
                       now_wall: Optional[float] = None) -> bool:
        """True when the group is not parked and has at least one ready URL."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        now_wall = time.time() if now_wall is None else now_wall
        if self._group_parked(group, now_wall):
            return False
        return any(self._proxy_ready(p, now_mono) for p in group.proxies)

    def _persist_parking(self, group: EgressGroup) -> None:
        """Write parking to ``ProxyDB`` when bound; never break the request path."""
        db = self.db
        save = getattr(db, "save_parking", None) if db is not None else None
        if save is None:
            return
        try:
            save(
                [p.url for p in group.proxies],
                group.quota_until,
                group.banned_until,
                group.permanently_banned,
            )
        except Exception as exc:
            logger.warning("ProxyDB parking write failed for %s: %s",
                           group.host, exc)

    def mark_quota_exhausted(self, group: EgressGroup, until: float) -> None:
        """Park this proxy's quota bucket (daily FreeUsageLimit) until an epoch.

        Only forward movement: a shorter value never shortens existing parking.
        ``mark_success``/health probes never clear it (only expiry does). The
        value is persisted immediately so a gateway restart keeps ``лимит`` in
        the admin panel.
        """
        group.quota_until = max(group.quota_until, float(until))
        self._persist_parking(group)

    def mark_banned(self, group: EgressGroup, until: float) -> None:
        """Ban this proxy's quota bucket temporarily until a wall-clock epoch."""
        group.banned_until = max(group.banned_until, float(until))
        self._persist_parking(group)

    def mark_permanently_banned(self, group: EgressGroup) -> None:
        """Ban an abusive-traffic bucket forever.

        The flag is persisted immediately and is never cleared by a successful
        probe, a refresh, a restart, or ``mark_success``; the bucket is skipped
        by every selection path.
        """
        group.permanently_banned = True
        self._persist_parking(group)

    def _ready_urls(self, group: EgressGroup, exclude: set[str],
                    now_mono: float) -> list[Proxy]:
        return [
            p for p in group.proxies
            if p.url not in exclude and self._proxy_ready(p, now_mono)
        ]

    def next_proxy(self, exclude: Optional[set[str]] = None) -> Proxy:
        """Uniform round-robin over ready proxies; never returns ``None``.

        Ready = enabled (in pool, not manually disabled), not disabled, not in
        cooldown, not on the RPM cap, not in ``exclude``, and its quota bucket
        is not parked (permanently banned, or ``quota_until`` / ``banned_until``
        in the future).
        The ``_rr`` index lives over the STABLE ``self.proxies`` list (not over
        the recomputed ready subset): each pick walks forward from ``_rr``,
        skipping non-ready entries, and leaves ``_rr`` right after the chosen
        entry — so a proxy is never picked twice in a row and never skipped
        when a neighbour cools down.

        CONTRACT: none ready -> ``NoReadyProxyError`` with ``kind`` =
        ``"empty"`` / ``"excluded"`` / ``"saturated"`` and ``retry_after`` =
        seconds until the earliest candidate frees up (cooldown / RPM slot /
        group quota/ban reset) or ``None`` when waiting is pointless.
        """
        exclude = exclude or set()
        now = time.monotonic()
        now_wall = time.time()
        n = len(self.proxies)
        if n == 0:
            raise NoReadyProxyError(None, "empty")
        for i in range(n):
            idx = (self._rr + i) % n
            p = self.proxies[idx]
            if not self._proxy_ready(p, now):
                continue
            group = self.group_of(p)
            if group is not None and self._group_parked(group, now_wall):
                continue
            if p.url in exclude:
                continue
            self._rr = (idx + 1) % n
            return p
        raise self._no_ready(exclude, now, now_wall)

    def _no_ready(self, exclude: set[str], now_mono: float,
                  now_wall: float) -> NoReadyProxyError:
        """Classify an exhausted pool (see ``NoReadyProxyError.kind``)."""
        eligible = [p for p in self.proxies if self._eligible(p)]
        if not eligible:
            return NoReadyProxyError(None, "empty")
        not_excluded = [p for p in eligible if p.url not in exclude]
        parked = any(
            g.is_parked(now_wall)
            and any(self._eligible(p) for p in g.proxies)
            for g in self._groups
        )
        if not not_excluded and not parked:
            return NoReadyProxyError(None, "excluded")
        wait = self._earliest_ready_seconds(exclude, now_mono, now_wall)
        if wait is None and not parked:
            return NoReadyProxyError(None, "excluded")
        return NoReadyProxyError(wait, "saturated")

    def next_proxy_in_group(self, group: EgressGroup,
                            exclude: Optional[set[str]] = None) -> Proxy:
        """Round-robin over ready URLs of one quota bucket.

        Unlike :meth:`next_proxy`, this never crosses group boundaries: affinity
        uses it to keep a session on its exit IP. Raises ``NoReadyProxyError``
        (``saturated``/``excluded``/``empty``) when the group itself is parked
        or has no ready non-excluded URL.
        """
        exclude = exclude or set()
        now = time.monotonic()
        now_wall = time.time()
        if not group.proxies:
            raise NoReadyProxyError(None, "empty")
        if self._group_parked(group, now_wall):
            if group.permanently_banned:
                raise NoReadyProxyError(None, "saturated")
            wait = max(group.parked_until() - now_wall, 0.0)
            url_wait = self._group_url_wait(group, exclude, now)
            raise NoReadyProxyError(max(wait, url_wait), "saturated")
        n = len(group.proxies)
        for i in range(n):
            idx = (group._rr + i) % n
            p = group.proxies[idx]
            if p.url in exclude or not self._proxy_ready(p, now):
                continue
            group._rr = (idx + 1) % n
            return p
        if any(self._eligible(p) for p in group.proxies if p.url not in exclude):
            raise NoReadyProxyError(
                self._group_url_wait(group, exclude, now), "saturated")
        raise NoReadyProxyError(None, "excluded")

    def _group_url_wait(self, group: EgressGroup, exclude: set[str],
                        now_mono: float) -> float:
        """Seconds until some URL of ``group`` is ready (parking not included)."""
        waits: list[float] = []
        for p in group.proxies:
            if not self._eligible(p) or p.url in exclude:
                continue
            waits.append(max(p.cooldown_until - now_mono,
                             self._rpm_wait(p, now_mono), 0.0))
        return min(waits) if waits else 0.0

    def earliest_ready_seconds(self, exclude: Optional[set[str]] = None) -> Optional[float]:
        """Public ``retry_after`` estimate (dual clock, includes parked groups).

        ``None`` = nothing sensible to wait for (pool empty or every candidate
        already excluded). Parked groups are included even when all their URLs
        are in ``exclude``: the caller may be out of fresh URLs but the group's
        quota/ban reset is still the earliest moment the request can proceed.
        """
        return self._earliest_ready_seconds(
            exclude or set(), time.monotonic(), time.time())

    def _earliest_ready_seconds(self, exclude: set[str], now: float,
                                now_wall: Optional[float] = None) -> Optional[float]:
        """``retry_after``: seconds until the earliest candidate becomes ready.

        Two clocks are mixed explicitly: cooldown/RPM use ``now`` (monotonic),
        quota/ban use ``time.time()``. For a parked group the wait is
        ``max(parking_reset, earliest URL cooldown/RPM)`` and its URLs are
        considered even if they are in ``exclude`` (see
        :meth:`earliest_ready_seconds`). ``None`` = no eligible candidate.
        """
        if now_wall is None:
            now_wall = time.time()
        best: Optional[float] = None
        for group in self._groups:
            if not any(self._eligible(p) for p in group.proxies):
                continue
            if group.permanently_banned:
                continue  # never wait for an abusive-traffic bucket
            parked_wait = max(group.parked_until() - now_wall, 0.0)
            if parked_wait > 0:
                url_wait = self._group_url_wait(group, set(), now)
                wait = max(parked_wait, url_wait)
            else:
                waits = [
                    max(p.cooldown_until - now, self._rpm_wait(p, now), 0.0)
                    for p in group.proxies
                    if p.url not in exclude and self._eligible(p)
                ]
                if not waits:
                    continue
                wait = min(waits)
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
        ``proxies_enabled`` — manually enabled (not dead); ``proxies_ready`` —
        actually selectable now (enabled, not cooling/RPM-capped, quota bucket
        not parked); ``proxies_cooling`` — in cooldown; ``proxies_rpm_capped``
        — hit the local rolling RPM cap (may overlap with cooling). Per-proxy:
        ``rpm_window`` — current RPM load, ``rpm_capped`` — whether capped,
        ``quota_seconds_left`` / ``banned_seconds_left`` — bucket parking,
        ``banned`` — permanent abusive-traffic ban (never selectable again).

        Additive egress-group contract: ``egress_groups_total`` /
        ``egress_groups_ready`` / ``egress_groups_quota_parked`` /
        ``egress_groups_banned`` plus an ``egress_groups`` list with host-only
        data (never credentials). One group == one proxy endpoint, so group
        quota fields are also mirrored on each proxy row for the admin UI.
        """
        now = time.monotonic()
        now_wall = time.time()
        enabled = [p for p in self.proxies if p.enabled and not p.disabled]
        groups = self.egress_groups()
        group_ready = [self.group_is_ready(g, now_mono=now, now_wall=now_wall)
                       for g in groups]
        proxy_rows = []
        for p in self.proxies:
            group = self.group_of(p)
            proxy_rows.append({
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
                "egress_group": group.host if group else None,
                "quota_seconds_left": (
                    round(max(0.0, group.quota_until - now_wall), 1) if group else 0.0
                ),
                "banned": bool(group and group.permanently_banned),
                "banned_seconds_left": (
                    0.0 if group and group.permanently_banned
                    else (round(max(0.0, group.banned_until - now_wall), 1)
                          if group else 0.0)
                ),
                "last_error": p.last_error,
            })
        return {
            "provider": PROVIDER_NAME,
            "proxies_total": len(self.proxies),
            "proxies_enabled": len(enabled),
            "proxies_ready": sum(
                1 for p in enabled
                if self._proxy_ready(p, now)
                and (group := self.group_of(p)) is not None
                and not self._group_parked(group, now_wall)
            ),
            "proxies_dead": sum(1 for p in self.proxies if p.disabled),
            "proxies_manually_disabled": sum(1 for p in self.proxies if p.manually_disabled),
            "proxies_cooling": sum(1 for p in enabled if p.cooldown_until > now),
            "proxies_rpm_capped": sum(1 for p in enabled if self._rpm_capped(p, now)),
            "egress_groups_total": len(groups),
            "egress_groups_ready": sum(1 for ok in group_ready if ok),
            "egress_groups_quota_parked": sum(1 for g in groups if g.quota_until > now_wall),
            "egress_groups_banned": sum(
                1 for g in groups
                if g.permanently_banned or g.banned_until > now_wall),
            # Defensive: never expose userinfo/DSN credentials in the snapshot.
            "last_proxies_fetch": redact_url(self.last_fetch_status) if self.last_fetch_status else None,
            "proxies": proxy_rows,
            "egress_groups": [
                {
                    "ip": g.host,
                    "host": g.host,
                    "proxies": [mask_proxy(p.url) for p in g.proxies],
                    "proxy_count": len(g.proxies),
                    "quota_seconds_left": round(max(0.0, g.quota_until - now_wall), 1),
                    "banned": g.permanently_banned,
                    "banned_seconds_left": (
                        0.0 if g.permanently_banned
                        else round(max(0.0, g.banned_until - now_wall), 1)
                    ),
                    "ready": ready,
                }
                for g, ready in zip(groups, group_ready)
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

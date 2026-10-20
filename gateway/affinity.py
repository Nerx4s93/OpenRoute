"""Sticky sessions over egress groups with score balancing (S + R + T).

One chat conversation is bound **permanently** to one purchased exit IP (an
:class:`gateway.proxies.EgressGroup`): the binding is never moved because another
IP became less loaded. Only a quota reset, an abusive ban, or an admin
delete/disable invalidates it.

New sessions (and sessions evacuated from a parked group) are balanced with:

- ``S`` — number of bound sessions per group (completed sessions keep their
  slot; this is the plan's default, see ``_aggregate``),
- ``R`` — client requests in the session's rolling window,
- ``T`` — upstream ``total_tokens`` in the same window.

Each session's window starts at ``max(created_at, now - window_seconds)`` and
is never extrapolated. A request gets exactly ONE :class:`RequestEvent`: it is
appended by :meth:`AffinityManager.note_started` on the first real upstream
attempt (``tokens=0``) and the same event is updated by ``note_tokens`` once
real upstream usage is known. This module is single-event-loop state (like
``KeyStore`` / ``SessionRegistry``); multi-worker deployments would need a
shared store.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .config import Settings
from .proxies import EgressGroup, NoReadyProxyError, Proxy, ProxyManager, mask_proxy

logger = logging.getLogger(__name__)

#: Safety cap on one session's event deque (a pathological RPM is bounded).
MAX_EVENTS_PER_SESSION = 10_000


@dataclass
class SessionIdentity:
    """Resolved client identity for one request.

    ``key`` is ``sid:<session_id>`` (valid caller header), ``fp:<sha256>``
    (conversation fingerprint) or ``None`` (one-shot: no binding, no row).
    """

    session_id: str
    key: str | None
    kind: str  # "sid" | "fp" | "one-shot"

    @property
    def is_one_shot(self) -> bool:
        return self.key is None


@dataclass
class RequestEvent:
    """One client request in a session's rolling window (wall-clock ``ts``)."""

    ts: float
    tokens: int = 0


@dataclass
class SessionState:
    """In-memory binding + rolling window for one ``session_key``."""

    key: str
    proxy: str | None
    created_at: float
    last_used_at: float
    events: deque[RequestEvent] = field(default_factory=deque, repr=False)


@dataclass
class AffinityLease:
    """Per-request handle produced by :meth:`AffinityManager.begin_request`.

    ``bound_proxy_url``/``group`` are the *permanent* binding (temporary
    fallback does not change them). ``event`` is the single request event
    appended by ``note_started``; ``started`` makes that call idempotent.
    """

    identity: SessionIdentity
    state: SessionState | None
    bound_proxy_url: str | None
    group: EgressGroup | None
    event: RequestEvent | None = None
    started: bool = False


class AffinityManager:
    """Session bindings, rolling-window statistics and score selection.

    All public state mutations (``begin_request`` / ``next_proxy`` /
    ``note_*`` / ``_evacuate_sync``) are synchronous: no ``await`` happens
    between reading and writing affinity state in the single event loop. Only
    PostgreSQL writes and :meth:`load` / :meth:`aclose` are asynchronous; they
    go through a coalescing background writer.
    """

    def __init__(
        self,
        settings: Settings,
        proxies: ProxyManager,
        db=None,
        now_wall=time.time,
        now_mono=time.monotonic,
    ) -> None:
        self._s = settings
        self.proxies = proxies
        self.db = db
        self._now_wall = now_wall
        self._now_mono = now_mono
        self._sessions: dict[str, SessionState] = {}
        self._oneshot: dict[str, deque[RequestEvent]] = {}
        self._rr = 0  # tie-break round-robin over stable candidate groups
        self._pending: dict[str, tuple[str, str | None, float, float]] = {}
        self._writer_task: Optional[asyncio.Task] = None
        self._loaded = False
        self._closed = False

    # --- lifecycle ---------------------------------------------------------

    async def load(self) -> None:
        """Load persisted bindings (S is restored; R/T start empty)."""
        if self.db is None or self._loaded:
            return
        try:
            rows = await asyncio.to_thread(self.db.load_all)
        except Exception:
            logger.exception("AffinityDB load failed; starting with empty bindings")
            return
        for key, proxy, created_at, last_used_at in rows:
            self._sessions.setdefault(
                key, SessionState(key, proxy, float(created_at), float(last_used_at))
            )
        self._loaded = True

    async def aclose(self) -> None:
        """Flush pending upserts, then close the DB (idempotent)."""
        if self._closed:
            return
        try:
            await self.flush()
        finally:
            self._closed = True
            task, self._writer_task = self._writer_task, None
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            db, self.db = self.db, None
            if db is not None:
                try:
                    await asyncio.to_thread(db.close)
                except Exception:
                    logger.debug("AffinityDB close failed", exc_info=True)

    async def flush(self) -> None:
        """Wait until every queued binding write reached PostgreSQL.

        The writer task is never awaited directly: it may have been created on
        a different event loop (multiple ``TestClient`` portals in tests).
        Awaiting a foreign task raises, while its ``done()`` flag and the
        pending queue are safe to poll. A foreign writer drains the queue until
        it is empty, so waiting for ``done()`` also waits for its last commit.
        """
        while not self._closed:
            task = self._writer_task
            if task is not None and not task.done():
                await asyncio.sleep(0.01)
                continue
            if not self._pending:
                break
            self._kick_writer()
            if self._writer_task is None:
                break  # no DB / closed: nothing will ever be written
            await asyncio.sleep(0.01)

    # --- coalescing DB writer ---------------------------------------------

    def _schedule_write(self, state: SessionState) -> None:
        if self.db is None or self._closed:
            return
        self._pending[state.key] = (
            state.key, state.proxy, state.created_at, state.last_used_at,
        )
        self._kick_writer()

    def _kick_writer(self) -> None:
        if self.db is None or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # synchronous unit-test context: no writer
        if self._writer_task is None or self._writer_task.done():
            self._writer_task = loop.create_task(self._writer_loop())

    @staticmethod
    def _is_fk_violation(exc: BaseException) -> bool:
        return exc.__class__.__name__ == "ForeignKeyViolation"

    async def _writer_loop(self) -> None:
        """Ordered, coalescing writer: latest state per session key wins.

        Concurrent requests may enqueue the same key several times; only the
        last snapshot is written. A FK violation means the proxy row was just
        deleted (race with an admin delete): the affected bindings are retried
        as ``proxy = NULL`` instead of losing the write.
        """
        try:
            while self._pending:
                batch = list(self._pending.values())
                self._pending.clear()
                try:
                    await asyncio.to_thread(self.db.upsert_many, batch)
                except Exception as exc:
                    if not self._is_fk_violation(exc):
                        logger.warning("AffinityDB upsert failed: %s", exc)
                        continue
                    # A referenced proxy was deleted concurrently. Retry ROW BY
                    # ROW: only the binding(s) that still point at a vanished
                    # proxy may become NULL. A batch-wide NULL retry would
                    # silently unbind healthy sessions that merely shared the
                    # coalesced write.
                    await self._retry_fk_batch(batch)
        finally:
            self._writer_task = None

    async def _retry_fk_batch(
        self, batch: list[tuple[str, str | None, float, float]]
    ) -> None:
        """Row-by-row fallback after a batch FK violation (deleted proxy)."""
        for key, proxy, created_at, used in batch:
            row = (key, proxy, created_at, used)
            try:
                await asyncio.to_thread(self.db.upsert_many, [row])
            except Exception as exc:
                if not self._is_fk_violation(exc):
                    logger.warning("AffinityDB upsert failed: %s", exc)
                    continue
                null_row = (key, None, created_at, used)
                try:
                    await asyncio.to_thread(self.db.upsert_many, [null_row])
                except Exception:
                    logger.warning("AffinityDB NULL retry failed", exc_info=True)
                    continue
                # The proxy vanished from PostgreSQL: keep memory in sync (only
                # if this state still points at the vanished binding).
                state = self._sessions.get(key)
                if (state is not None and state.proxy == proxy
                        and state.last_used_at == used):
                    state.proxy = None

    # --- window / aggregates ----------------------------------------------

    def _window_start(self, created_at: float, now: float) -> float:
        return max(created_at, now - float(self._s.affinity_window_seconds))

    def _prune_events(self, state: SessionState, now: float) -> None:
        cutoff = now - float(self._s.affinity_window_seconds)
        events = state.events
        while events and events[0].ts < cutoff:
            events.popleft()
        while len(events) > MAX_EVENTS_PER_SESSION:
            events.popleft()

    def session_stats(self, state: SessionState,
                      now: Optional[float] = None) -> tuple[int, int]:
        """``(requests_1h, tokens_1h)`` for one session (no extrapolation)."""
        now = self._now_wall() if now is None else now
        start = self._window_start(state.created_at, now)
        reqs = toks = 0
        for event in state.events:
            if event.ts >= start:
                reqs += 1
                toks += max(0, int(event.tokens))
        return reqs, toks

    def _prune_oneshot(self, group: EgressGroup, now: float) -> None:
        buf = self._oneshot.get(group.host)
        if buf is None:
            return
        cutoff = now - float(self._s.affinity_window_seconds)
        while buf and buf[0].ts < cutoff:
            buf.popleft()

    def _oneshot_stats(self, group: EgressGroup, now: float) -> tuple[int, int]:
        self._prune_oneshot(group, now)
        reqs = 0
        toks = 0
        for event in self._oneshot.get(group.host, ()):
            reqs += 1
            toks += max(0, int(event.tokens))
        return reqs, toks

    def _sessions_in(self, group: EgressGroup) -> list[SessionState]:
        out = []
        for state in self._sessions.values():
            if state.proxy and self.proxies.group_of(state.proxy) is group:
                out.append(state)
        return out

    def _aggregate(self, group: EgressGroup,
                   extra: Optional[SessionState] = None) -> tuple[int, int, int]:
        """``(S, R, T)`` for a group.

        **S = all bound sessions, including completed ones** (plan §13.1). To
        switch to active-only later, filter ``state.last_used_at`` here; the
        rest of the scoring schema does not change.
        """
        now = self._now_wall()
        sessions = self._sessions_in(group)
        s_count = len(sessions)
        reqs = toks = 0
        for state in sessions:
            r, t = self.session_stats(state, now)
            reqs += r
            toks += t
        # Temporary fallback stats stay on the permanent group; an ``extra``
        # session contributes its projected load to a *candidate* group.
        if extra is not None and self.proxies.group_of(extra.proxy) is not group:
            r, t = self.session_stats(extra, now)
            s_count += 1
            reqs += r
            toks += t
        one_r, one_t = self._oneshot_stats(group, now)
        return s_count, reqs + one_r, toks + one_t

    def group_stats(self, group: EgressGroup) -> tuple[int, int, int]:
        """Public ``(S, R, T)`` snapshot for one egress group."""
        return self._aggregate(group)

    def _scores(self, candidates: list[EgressGroup],
                extra: Optional[SessionState] = None) -> dict[str, float]:
        """Weighted normalised score per candidate (maxima among candidates)."""
        aggs = {g.host: self._aggregate(g, extra) for g in candidates}
        smax = max((a[0] for a in aggs.values()), default=0)
        rmax = max((a[1] for a in aggs.values()), default=0)
        tmax = max((a[2] for a in aggs.values()), default=0)
        ws = float(self._s.affinity_weight_sessions)
        wr = float(self._s.affinity_weight_requests)
        wt = float(self._s.affinity_weight_tokens)
        out: dict[str, float] = {}
        for host, (s_count, reqs, toks) in aggs.items():
            out[host] = (
                (ws * s_count / smax if smax else 0.0)
                + (wr * reqs / rmax if rmax else 0.0)
                + (wt * toks / tmax if tmax else 0.0)
            )
        return out

    def score(self, group: EgressGroup,
              extra: Optional[SessionState] = None,
              candidates: Optional[Iterable[EgressGroup]] = None) -> float:
        """Score one group; maxima are computed over ``candidates``.

        With no ``candidates`` argument every egress group participates; the
        selection path always passes its ready candidate list explicitly.
        """
        cands = list(candidates) if candidates is not None else self.proxies.egress_groups()
        if group not in cands:
            cands.append(group)
        return self._scores(cands, extra).get(group.host, 0.0)

    # --- selection ---------------------------------------------------------

    def _group_has_url(self, group: EgressGroup, exclude: set[str],
                       now_mono: Optional[float] = None,
                       now_wall: Optional[float] = None) -> bool:
        now_mono = self._now_mono() if now_mono is None else now_mono
        now_wall = self._now_wall() if now_wall is None else now_wall
        if not self.proxies.group_is_ready(
                group, now_mono=now_mono, now_wall=now_wall):
            return False
        return any(
            p.url not in exclude and self.proxies._proxy_ready(p, now_mono)
            for p in group.proxies
        )

    def _candidates(self, exclude: set[str]) -> list[EgressGroup]:
        now_mono = self._now_mono()
        now_wall = self._now_wall()
        return [
            g for g in self.proxies.egress_groups()
            if self._group_has_url(g, exclude, now_mono, now_wall)
        ]

    def _pick_group(self, exclude: set[str],
                    extra: Optional[SessionState] = None) -> Optional[EgressGroup]:
        """Lowest projected score; stable round-robin tie-break."""
        candidates = self._candidates(exclude)
        if not candidates:
            return None
        scores = self._scores(candidates, extra)
        best = min(scores.values())
        tied = [g for g in candidates if abs(scores[g.host] - best) <= 1e-12]
        group = tied[self._rr % len(tied)]
        self._rr += 1
        return group

    def _pick_url(self, exclude: set[str]) -> Optional[Proxy]:
        group = self._pick_group(exclude)
        if group is None:
            return None
        try:
            return self.proxies.next_proxy_in_group(group, exclude)
        except NoReadyProxyError:
            return None

    @staticmethod
    def _find_proxy(proxies: ProxyManager, url: str | None) -> Optional[Proxy]:
        if not url:
            return None
        for proxy in proxies.proxies:
            if proxy.url == url:
                return proxy
        return None

    def _bind_state(self, state: SessionState, url: str) -> None:
        state.proxy = url
        state.last_used_at = self._now_wall()
        self._schedule_write(state)

    def _bind_best(self, state: SessionState, exclude: set[str]) -> bool:
        proxy = self._pick_url(exclude)
        if proxy is None:
            return False
        self._bind_state(state, proxy.url)
        return True

    def _unbind_state(self, state: SessionState) -> None:
        state.proxy = None
        state.last_used_at = self._now_wall()
        self._schedule_write(state)

    def _reconcile(self, state: SessionState,
                   exclude: Optional[set[str]] = None) -> None:
        """Lazily validate the permanent binding against the runtime pool.

        - URL missing / disabled / manually disabled -> rebind (prefer another
          purchased port of the SAME exit IP when it exists),
        - whole group parked -> evacuate it synchronously,
        - no proxy at all -> try to bind by score.
        A merely cooling/RPM-capped URL stays bound: that is a temporary
        fallback for the current request, not a permanent move.
        """
        exclude = set(exclude or ())
        now_wall = self._now_wall()
        if not state.proxy:
            self._bind_best(state, exclude)
            return
        proxy = self._find_proxy(self.proxies, state.proxy)
        group = self.proxies.group_of(state.proxy)
        if proxy is None or not proxy.enabled or proxy.disabled or proxy.manually_disabled:
            target: Optional[Proxy] = None
            if group is not None:
                try:
                    target = self.proxies.next_proxy_in_group(group, exclude)
                except NoReadyProxyError:
                    target = None
            if target is None:
                target = self._pick_url(exclude)
            if target is None:
                self._unbind_state(state)
            else:
                self._bind_state(state, target.url)
            return
        if group is not None and group.parked_until() > now_wall:
            self._evacuate_sync(group, exclude=exclude)

    # --- per-request API ---------------------------------------------------

    def begin_request(self, identity: SessionIdentity) -> AffinityLease:
        """Create/refresh the binding and return this request's lease.

        A new session is assigned by score; an existing session keeps its
        binding (and only refreshes ``last_used_at``). One-shot requests carry
        no :class:`SessionState` and never create a DB row.
        """
        now = self._now_wall()
        if identity.key is None:
            return AffinityLease(identity, None, None, None)
        state = self._sessions.get(identity.key)
        if state is None:
            state = SessionState(identity.key, None, now, now)
            self._sessions[identity.key] = state
            self._bind_best(state, set())
        else:
            state.last_used_at = now
            self._reconcile(state)
            self._schedule_write(state)
        group = self.proxies.group_of(state.proxy) if state.proxy else None
        return AffinityLease(identity, state, state.proxy, group)

    def next_proxy(self, lease: AffinityLease,
                   exclude: Optional[set[str]] = None) -> Proxy:
        """Pick the URL for this attempt.

        - one-shot: score-aware group choice (no permanent binding),
        - bound URL ready: exactly that URL,
        - bound URL only cooling/RPM-capped: temporary fallback to another
          ready URL/group, the permanent binding is unchanged,
        - URL missing/disabled/group gone: permanent rebind by score,
        - bound group parked: evacuate it first, then use the new binding.
        """
        exclude = set(exclude or ())
        if lease.identity.key is None or lease.state is None:
            group = self._pick_group(exclude)
            if group is None:
                # Raise the correctly classified NoReadyProxyError.
                return self.proxies.next_proxy(exclude)
            lease.group = group
            try:
                return self.proxies.next_proxy_in_group(group, exclude)
            except NoReadyProxyError:
                return self.proxies.next_proxy(exclude)

        state = lease.state
        now_mono = self._now_mono()
        now_wall = self._now_wall()
        # Invalidation is permanent (missing/disabled/parked), so ignore this
        # request's ``exclude`` here: a stale binding must still be rebound.
        self._reconcile(state, exclude=set())
        lease.bound_proxy_url = state.proxy
        lease.group = self.proxies.group_of(state.proxy) if state.proxy else None

        proxy = self._find_proxy(self.proxies, state.proxy)
        group = lease.group
        if (proxy is not None and group is not None
                and group.parked_until() <= now_wall
                and self.proxies._proxy_ready(proxy, now_mono)
                and proxy.url not in exclude):
            return proxy

        if group is not None and group.parked_until() > now_wall:
            self._evacuate_sync(group, exclude=exclude)
            lease.bound_proxy_url = state.proxy
            lease.group = self.proxies.group_of(state.proxy) if state.proxy else None
            proxy = self._find_proxy(self.proxies, state.proxy)
            group = lease.group
            if (proxy is not None and group is not None
                    and group.parked_until() <= now_wall
                    and self.proxies._proxy_ready(proxy, now_mono)
                    and proxy.url not in exclude):
                return proxy

        # Temporary fallback (cooling / RPM cap / URL already tried): the
        # permanent binding in ``state`` and PostgreSQL stays untouched.
        chosen = self._pick_url(exclude)
        if chosen is not None:
            return chosen
        # No candidate: raise with the pool's classification/retry_after.
        return self.proxies.next_proxy(exclude)

    def note_started(self, lease: AffinityLease, proxy: Proxy, ts: float) -> None:
        """Append the ONE request event at the first real upstream attempt.

        Idempotent: retries inside the same client request must not add more
        events. ``tokens`` starts at 0 so R lands in the window immediately;
        :meth:`note_tokens` later fills the same object.
        """
        if lease.started:
            return
        lease.started = True
        state = lease.state
        if state is not None:
            # A new session's ``created_at`` can be a few microseconds after
            # the request start captured by the caller (session creation is
            # part of request handling). Never place the first event outside
            # its own window: the window starts at ``created_at``.
            ts = max(float(ts), state.created_at)
        event = RequestEvent(ts=float(ts), tokens=0)
        lease.event = event
        if lease.identity.key is None or state is None:
            group = self.proxies.group_of(proxy) or lease.group
            lease.group = group
            if group is not None:
                self._oneshot.setdefault(group.host, deque()).append(event)
            return
        now = self._now_wall()
        state.last_used_at = max(state.last_used_at, now)
        self._prune_events(state, now)
        state.events.append(event)

    def note_tokens(self, lease: AffinityLease, tokens: int | None) -> None:
        """Fill real upstream usage into the event created by ``note_started``.

        Never creates a second event. Unknown usage is recorded as 0 (the
        event's initial value); callers must not pass synthetic ``bytes / 4``
        estimates here.
        """
        if lease.event is None or tokens is None:
            return
        try:
            value = int(tokens)
        except (TypeError, ValueError):
            return
        lease.event.tokens = max(0, value)

    # --- evacuation / invalidation ----------------------------------------

    def _evacuate_sync(self, group: EgressGroup, *,
                       quota_until: Optional[float] = None,
                       banned_until: Optional[float] = None,
                       exclude: Optional[set[str]] = None) -> list[SessionState]:
        """Park + move sessions in-memory without a single ``await``.

        Big sessions first (``tokens_1h`` desc, then ``requests_1h`` desc,
        then key desc); after every assignment aggregates are recomputed, so
        the next session sees the projector's new load. DB writes are queued
        and coalesced.
        """
        exclude = set(exclude or ())
        now = self._now_wall()
        if quota_until is not None:
            self.proxies.mark_quota_exhausted(group, quota_until)
        if banned_until is not None:
            self.proxies.mark_banned(group, banned_until)

        moving = [
            s for s in self._sessions.values()
            if s.proxy and self.proxies.group_of(s.proxy) is group
        ]

        def sort_key(s: SessionState) -> tuple[int, int, str]:
            r, t = self.session_stats(s, now)
            return t, r, s.key

        moving.sort(key=sort_key, reverse=True)
        for state in moving:
            candidates = [
                g for g in self.proxies.egress_groups()
                if g is not group and self._group_has_url(g, exclude)
            ]
            if not candidates:
                self._unbind_state(state)
                continue
            scores = self._scores(candidates, extra=state)
            best = min(candidates, key=lambda g: scores[g.host])
            try:
                proxy = self.proxies.next_proxy_in_group(best, exclude)
            except NoReadyProxyError:
                self._unbind_state(state)
                continue
            self._bind_state(state, proxy.url)
        return moving

    async def evacuate(self, group: EgressGroup, *,
                       quota_until: Optional[float] = None,
                       banned_until: Optional[float] = None,
                       exclude: Optional[set[str]] = None) -> list[SessionState]:
        """Async wrapper around :meth:`_evacuate_sync`.

        Parking and every in-memory rebind happen synchronously inside the
        call (before it returns control to the event loop); only the coalesced
        PostgreSQL write is backgrounded.
        """
        return self._evacuate_sync(
            group, quota_until=quota_until, banned_until=banned_until,
            exclude=exclude,
        )

    def forget_proxy(self, url: str) -> int:
        """Invalidate every binding to ``url`` (admin delete/disable hook).

        The session is unbound (``proxy = NULL``); its next request rebinds by
        score. Returns the number of affected sessions.
        """
        affected = 0
        for state in self._sessions.values():
            if state.proxy == url:
                self._unbind_state(state)
                affected += 1
        return affected

    # --- status ------------------------------------------------------------

    def status(self) -> dict:
        """Host-only affinity snapshot (never full proxy URLs/credentials)."""
        now = self._now_wall()
        bound = [s for s in self._sessions.values() if s.proxy]
        unbound = [s for s in self._sessions.values() if not s.proxy]
        groups = []
        all_groups = self.proxies.egress_groups()
        scores = self._scores(all_groups) if all_groups else {}
        for group in all_groups:
            s_count, reqs, toks = self._aggregate(group)
            groups.append({
                "ip": group.host,
                "host": group.host,
                "proxies": [mask_proxy(p.url) for p in group.proxies],
                "proxy_count": len(group.proxies),
                "sessions": s_count,
                "requests_1h": reqs,
                "tokens_1h": toks,
                "score": round(scores.get(group.host, 0.0), 6),
                "ready": self.proxies.group_is_ready(group, now_wall=now),
                "quota_seconds_left": round(max(0.0, group.quota_until - now), 1),
                "banned_seconds_left": round(max(0.0, group.banned_until - now), 1),
            })
        return {
            "enabled": bool(self._s.affinity_enabled),
            "window_seconds": float(self._s.affinity_window_seconds),
            "sessions_total": len(self._sessions),
            "sessions_bound": len(bound),
            "sessions_unbound": len(unbound),
            "egress_groups": groups,
        }

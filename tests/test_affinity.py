"""Unit tests for gateway.affinity (no network, no PostgreSQL required).

Covers score balancing, the 1-hour window, sticky bindings, temporary
fallback, one-shot buffers and evacuation.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from gateway.affinity import (
    AffinityManager,
    RequestEvent,
    SessionIdentity,
    SessionState,
)
from gateway.proxies import ProxyManager

from gw import build_settings

_UNUSED_DSN = "postgresql://unused"


class Clock:
    def __init__(self):
        self.wall = time.time()
        self.mono = time.monotonic()

    def now_wall(self):
        return self.wall

    def now_mono(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1", "http://c.invalid:1"),
                  clock=None, **over):
    clock = clock or Clock()
    settings = build_settings(_UNUSED_DSN, **over)
    manager = ProxyManager(settings)
    manager.apply_proxies([(p, True) for p in proxies])
    aff = AffinityManager(settings, manager, now_wall=clock.now_wall,
                          now_mono=clock.now_mono)
    return aff, manager, clock


def identity(sid: str) -> SessionIdentity:
    return SessionIdentity(sid, f"sid:{sid}", "sid")


def seed_session(aff, key, proxy, events=(), created=None, last=None):
    clock = aff._now_wall()
    state = SessionState(
        key=key,
        proxy=proxy,
        created_at=clock if created is None else created,
        last_used_at=clock if last is None else last,
        events=deque_events(events),
    )
    aff._sessions[key] = state
    return state


def deque_events(events):
    from collections import deque
    return deque(RequestEvent(ts=t, tokens=n) for t, n in events)


def bind(aff, sid):
    return aff.begin_request(identity(sid))


# --- score -----------------------------------------------------------------


def test_score_matches_plan_example():
    """IP1: S=2,R=3000 vs IP2: S=3,R=1000 -> IP2 wins (1.333 < 1.667)."""
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1"))
    a, b = manager.egress_groups()
    for i in range(2):
        seed_session(aff, f"a{i}", a.proxies[0].url)
    for i in range(3):
        seed_session(aff, f"b{i}", b.proxies[0].url)
    now = clock.wall
    aff._oneshot[a.host] = deque_events([(now, 0)] * 3000)
    aff._oneshot[b.host] = deque_events([(now, 0)] * 1000)

    assert aff.score(a) == pytest.approx(2 / 3 + 1.0)
    assert aff.score(b) == pytest.approx(1.0 + 1 / 3)
    assert aff._pick_group(set()) is b


def test_score_zero_maxima_round_robin_evenly():
    aff, manager, _ = make_affinity()
    groups = manager.egress_groups()
    for i in range(30):
        bind(aff, f"ses{i}")
    counts = {g.host: sum(1 for s in aff._sessions.values()
                          if manager.group_of(s.proxy) is g) for g in groups}
    assert list(counts.values()) == [10, 10, 10]


def test_weight_sessions_only_picks_least_sessions():
    aff, manager, _ = make_affinity(
        proxies=("http://a.invalid:1", "http://b.invalid:1"),
        affinity_weight_sessions=10.0,
        affinity_weight_requests=0.0,
        affinity_weight_tokens=0.0,
    )
    a, b = manager.egress_groups()
    seed_session(aff, "a0", a.proxies[0].url)
    seed_session(aff, "b0", b.proxies[0].url)
    seed_session(aff, "b1", b.proxies[0].url)
    assert aff._pick_group(set()) is a


def test_tie_break_does_not_stick_to_one_group():
    aff, manager, _ = make_affinity()
    picks = [aff._pick_group(set()).host for _ in range(6)]
    assert picks == ["a.invalid:1", "b.invalid:1", "c.invalid:1"] * 2


def test_new_session_skips_parked_group():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1"))
    a, b = manager.egress_groups()
    manager.mark_quota_exhausted(a, clock.wall + 3600)
    for i in range(12):
        lease = bind(aff, f"ses{i}")
        assert manager.group_of(lease.state.proxy) is b


# --- one-hour window -------------------------------------------------------


def test_window_young_session_counts_everything():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1",))
    proxy = manager.proxies[0].url
    now = clock.wall
    state = seed_session(aff, "s", proxy, created=now - 600,
                         events=[(now - 500, 10), (now - 400, 20),
                                 (now - 200, 0), (now - 100, 5), (now, 1)])
    assert aff.session_stats(state) == (5, 36)


def test_window_old_session_keeps_only_last_hour_with_boundary():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1",))
    proxy = manager.proxies[0].url
    now = clock.wall
    state = seed_session(
        aff, "s", proxy, created=now - 3 * 3600,
        events=[(now - 2 * 3600, 100), (now - 3600, 7),
                (now - 1800, 3), (now - 1, 4)],
    )
    # ts exactly at window_start is included; older event is not.
    assert aff.session_stats(state, now=now) == (3, 7 + 3 + 4)


def test_window_boundary_can_be_created_at_for_young_session():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1",))
    now = clock.wall
    state = seed_session(aff, "s", manager.proxies[0].url, created=now - 60,
                         events=[(now - 600, 9), (now - 60, 2)])
    # window starts at created_at when the session is younger than one hour
    assert aff.session_stats(state, now=now) == (1, 2)


# --- request events --------------------------------------------------------


def test_note_started_is_idempotent_and_note_tokens_updates_same_event():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1",))
    lease = bind(aff, "s1")
    proxy = manager.proxies[0]
    aff.note_started(lease, proxy, clock.wall)
    aff.note_started(lease, proxy, clock.wall + 1)  # retry attempt: no new event
    assert len(lease.state.events) == 1
    event = lease.state.events[0]

    aff.note_tokens(lease, 42)
    aff.note_tokens(lease, 42)  # idempotent assignment, no new event
    assert len(lease.state.events) == 1
    assert event.tokens == 42
    assert aff.session_stats(lease.state) == (1, 42)


def test_first_event_never_falls_before_session_created_at():
    """Request start may precede session creation by microseconds.

    The event must still be inside the session window (otherwise the very
    first request would be invisible to R/T scoring).
    """
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1",))
    lease = bind(aff, "s1")
    aff.note_started(lease, manager.proxies[0], lease.state.created_at - 0.001)
    assert aff.session_stats(lease.state) == (1, 0)
    assert lease.event.ts >= lease.state.created_at


def test_failed_request_counts_r_but_zero_tokens():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1",))
    lease = bind(aff, "s1")
    aff.note_started(lease, manager.proxies[0], clock.wall)
    aff.note_tokens(lease, 0)
    assert aff.session_stats(lease.state) == (1, 0)


def test_note_tokens_without_event_does_nothing():
    aff, manager, _ = make_affinity(proxies=("http://a.invalid:1",))
    lease = aff.begin_request(SessionIdentity("ses_x", None, "one-shot"))
    aff.note_tokens(lease, 99)  # no upstream send happened
    assert lease.event is None and aff._oneshot == {}


# --- bindings --------------------------------------------------------------


def test_same_sid_keeps_same_proxy_and_refreshes_last_used():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1"))
    lease1 = bind(aff, "ses_same")
    first = lease1.state.proxy
    clock.advance(10)
    lease2 = bind(aff, "ses_same")
    assert lease2.state is lease1.state
    assert lease2.state.proxy == first
    assert lease2.state.last_used_at == clock.wall


def test_different_sessions_distribute():
    aff, manager, _ = make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1"))
    p1 = bind(aff, "ses_one").state.proxy
    p2 = bind(aff, "ses_two").state.proxy
    assert p1 != p2


def test_disabled_url_rebinds_permanently():
    aff, manager, _ = make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1"))
    lease = bind(aff, "ses_one")
    old = lease.state.proxy
    manager.set_manually_disabled(old, True)
    lease2 = aff.begin_request(identity("ses_one"))
    assert lease2.state.proxy != old
    assert manager.group_of(lease2.state.proxy).proxies[0].enabled


def test_forget_proxy_unbinds_and_next_request_rebinds():
    aff, manager, _ = make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1"))
    lease = bind(aff, "ses_one")
    old = lease.state.proxy
    assert aff.forget_proxy(old) == 1
    assert lease.state.proxy is None
    lease2 = aff.begin_request(identity("ses_one"))
    assert lease2.state.proxy is not None


def test_temporary_fallback_does_not_change_permanent_binding():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1"))
    lease = bind(aff, "ses_one")
    a = next(p for p in manager.proxies if p.url == lease.state.proxy)
    a.cooldown_until = time.monotonic() + 100.0

    # begin_request must NOT move the binding for a merely cooling URL.
    lease2 = aff.begin_request(identity("ses_one"))
    assert lease2.state.proxy == a.url
    chosen = aff.next_proxy(lease2, exclude=set())
    assert chosen.url != a.url
    assert lease2.state.proxy == a.url  # permanent binding unchanged
    assert lease2.bound_proxy_url == a.url

    a.cooldown_until = 0.0
    lease3 = aff.begin_request(identity("ses_one"))
    assert aff.next_proxy(lease3, exclude=set()).url == a.url


def test_temporary_fallback_can_use_another_url_without_rebinding():
    aff, manager, _ = make_affinity(proxies=(
        "http://10.0.0.1:1", "http://10.0.0.1:2", "http://10.0.0.2:3"))
    lease = bind(aff, "ses_one")
    old = next(p for p in manager.proxies if p.url == lease.state.proxy)
    old.cooldown_until = time.monotonic() + 100.0
    lease2 = aff.begin_request(identity("ses_one"))
    chosen = aff.next_proxy(lease2, exclude=set())
    assert chosen.url != old.url
    assert manager._proxy_ready(chosen, time.monotonic())
    assert lease2.state.proxy == old.url  # still the permanent URL


# --- one-shot --------------------------------------------------------------


def test_one_shot_has_no_state_but_fills_group_buffer():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1",))
    lease = aff.begin_request(SessionIdentity("ses_x", None, "one-shot"))
    assert lease.state is None and aff._sessions == {}
    proxy = aff.next_proxy(lease, exclude=set())
    group = manager.group_of(proxy)
    aff.note_started(lease, proxy, clock.wall)
    aff.note_tokens(lease, 7)
    assert aff.group_stats(group)[1:] == (1, 7)
    assert aff._sessions == {}


def test_one_shot_is_score_aware():
    aff, manager, _ = make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1"))
    lease = bind(aff, "ses_one")  # A gets S=1
    a = manager.group_of(lease.state.proxy)
    # One-shot should avoid A when B has S=0.
    for i in range(5):
        shot = aff.begin_request(SessionIdentity(f"ses_x{i}", None, "one-shot"))
        assert manager.group_of(aff.next_proxy(shot, set())) is not a


# --- evacuation ------------------------------------------------------------


@pytest.mark.asyncio
async def test_evacuation_big_sessions_first_and_recomputes_after_each():
    aff, manager, clock = make_affinity()
    a, b, c = manager.egress_groups()
    now = clock.wall
    for key, tokens in (("small", 10), ("big", 100), ("mid", 50)):
        seed_session(aff, key, a.proxies[0].url, events=[(now, tokens)])

    moved = await aff.evacuate(a, quota_until=now + 3600)
    assert [s.key for s in moved] == ["big", "mid", "small"]  # refill order
    assert a.quota_until == pytest.approx(now + 3600)
    for state in moved:
        assert manager.group_of(state.proxy) is not a
        assert manager.group_of(state.proxy) in (b, c)
    # LPT + projected score: the biggest goes first (B), mid sees B loaded -> C.
    by_key = {s.key: s for s in moved}
    assert manager.group_of(by_key["big"].proxy) is b
    assert manager.group_of(by_key["mid"].proxy) is c


@pytest.mark.asyncio
async def test_evacuation_without_candidates_unbinds():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1",))
    a = manager.egress_groups()[0]
    seed_session(aff, "s1", a.proxies[0].url, events=[(clock.wall, 5)])
    moved = await aff.evacuate(a, banned_until=clock.wall + 100)
    assert moved[0].proxy is None
    assert a.banned_until == pytest.approx(clock.wall + 100)


@pytest.mark.asyncio
async def test_evacuation_respects_tried_urls():
    aff, manager, clock = make_affinity(proxies=("http://a.invalid:1", "http://b.invalid:1"))
    a, b = manager.egress_groups()
    seed_session(aff, "s1", a.proxies[0].url, events=[(clock.wall, 5)])
    moved = await aff.evacuate(a, quota_until=clock.wall + 60,
                               exclude={b.proxies[0].url})
    assert moved[0].proxy is None  # B's only URL was already tried


# --- status ----------------------------------------------------------------


def test_status_is_host_only_and_never_leaks_credentials():
    aff, manager, clock = make_affinity(
        proxies=("http://user:supersecret@10.0.0.1:3128",))
    bind(aff, "ses_one")
    blob = str(aff.status())
    assert "supersecret" not in blob and "user:" not in blob
    group = aff.status()["egress_groups"][0]
    assert group["ip"] == "10.0.0.1:3128"
    assert group["sessions"] == 1
    assert group["proxy_count"] == 1
    assert group["score"] >= 0.0
    assert aff.status()["sessions_bound"] == 1
    assert aff.status()["sessions_unbound"] == 0


def test_status_counts_unbound():
    aff, manager, _ = make_affinity(proxies=("http://a.invalid:1",))
    state = seed_session(aff, "stale", None)
    assert aff.status()["sessions_unbound"] == 1
    state.proxy = manager.proxies[0].url
    assert aff.status()["sessions_bound"] == 1


# --- load ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_restores_bindings_without_events():
    class FakeDB:
        def load_all(self):
            return [("sid:old", "http://a.invalid:1", 100.0, 200.0)]

    aff, manager, _ = make_affinity(proxies=("http://a.invalid:1",))
    aff.db = FakeDB()
    await aff.load()
    state = aff._sessions["sid:old"]
    assert state.proxy == "http://a.invalid:1"
    assert state.created_at == 100.0 and state.last_used_at == 200.0
    assert list(state.events) == []


# --- writer / flush --------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_waits_for_a_writer_task_on_another_event_loop():
    """TestClient portals run on different loops; flush must still commit.

    A writer task created on a foreign loop can neither be awaited nor ignored:
    flush has to poll it to completion, otherwise a immediately following
    ``load_all()`` can read the previous row.
    """

    class SlowDB:
        def __init__(self):
            self.rows = []
            self.started = threading.Event()
            self.done = threading.Event()

        def upsert_many(self, rows):
            self.started.set()
            time.sleep(0.2)
            self.rows.extend(rows)
            self.done.set()

    db = SlowDB()
    aff, manager, _ = make_affinity(proxies=("http://a.invalid:1",))
    aff.db = db
    state = seed_session(aff, "sid:cross", manager.proxies[0].url)

    def foreign_loop():
        async def go():
            aff._schedule_write(state)  # creates the writer on THIS loop
            while not db.done.is_set():
                await asyncio.sleep(0.01)

        asyncio.run(go())

    thread = threading.Thread(target=foreign_loop)
    thread.start()
    try:
        assert await asyncio.to_thread(db.started.wait, 2.0)
        await aff.flush()
        assert db.done.is_set()
    finally:
        thread.join(5.0)
    assert db.rows == [("sid:cross", "http://a.invalid:1",
                        state.created_at, state.last_used_at)]


class ForeignKeyViolation(Exception):
    """Stands in for psycopg.errors.ForeignKeyViolation (name-matched)."""


class FkRejectingDB:
    """Fake AffinityDB whose proxy FK rejects rows with vanished URLs."""

    def __init__(self, missing):
        self.missing = set(missing)
        self.rows = []

    def upsert_many(self, rows):
        for _key, proxy, _created, _used in rows:
            if proxy in self.missing:
                raise ForeignKeyViolation(f"proxy row vanished: {proxy}")
        keys = {row[0] for row in rows}
        self.rows = [r for r in self.rows if r[0] not in keys] + list(rows)
        return len(rows)


@pytest.mark.asyncio
async def test_fk_violation_retry_nulls_only_the_vanished_binding():
    """One deleted proxy must not unbind healthy sessions in the same batch.

    The writer retries a FK-rejected batch row by row; a batch-wide NULL retry
    would silently wipe the live binding of every session that merely shared
    one coalesced write with the vanished proxy.
    """
    aff, _manager, _ = make_affinity(
        proxies=("http://a.invalid:1", "http://b.invalid:1"))
    aff.db = FkRejectingDB(missing={"http://a.invalid:1"})
    gone = seed_session(aff, "sid:1", "http://a.invalid:1", created=1.0, last=2.0)
    live = seed_session(aff, "sid:2", "http://b.invalid:1", created=1.0, last=2.0)
    aff._schedule_write(gone)
    aff._schedule_write(live)
    await aff.flush()

    assert {key: proxy for key, proxy, *_ in aff.db.rows} == {
        "sid:1": None, "sid:2": "http://b.invalid:1"}
    assert gone.proxy is None
    assert live.proxy == "http://b.invalid:1"

"""Tests for proxies.py: breaker (rolling window, exponential growth, cap),
429 cooldown, 429 parking (3 in a row -> 1 h), mark_success, next_proxy
(uniform RR, exclude, NoReadyProxyError), RPM cap, proxy parsing/dedupe, probe
behaviour (407/5xx), ensure_client, status/masking.

Most tests need no PostgreSQL (ProxyManager has no store); the refresh/upsert
integration tests use the ``pg_dsn`` fixture.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

import gateway.opencode as opencode
import gateway.proxies as prox
from gateway.proxies import NoReadyProxyError, Proxy, ProxyManager, mask_proxy

from gw import build_settings

_UNUSED_DSN = "postgresql://unused"


def mgr(proxies=(), **over):
    m = ProxyManager(build_settings(_UNUSED_DSN, **over))
    m.apply_proxies([(p, True) for p in proxies])
    return m


class _FakeResp:
    """Fake proxy fetcher response: arbitrary payload, always OK."""

    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeFetcher:
    """Replacement for self._fetcher: no network calls."""

    def __init__(self, payload):
        self.payload = payload
        self.url = None

    async def get(self, url):
        self.url = url
        return _FakeResp(self.payload)

    async def post(self, url, json=None):
        pass

    async def aclose(self):
        pass


class _StatusResp:
    def __init__(self, status):
        self.status_code = status


async def _fake_fetch(m, payload):
    """Swap the fetcher for _FakeFetcher(payload), close the old one (no leak)."""
    old = m._fetcher
    m._fetcher = _FakeFetcher(payload)
    await old.aclose()


async def _alive_probe(self, url):
    return True


# --- breaker: rolling window ---


def test_three_failures_in_window_cause_cooldown():
    m = mgr()
    p = Proxy("http://p:1")
    m.mark_failure(p, "e1")
    m.mark_failure(p, "e2")
    assert p.cooldown_until == 0.0 and p.consecutive_errors == 2 and len(p._fails) == 2

    m.mark_failure(p, "e3")
    assert p.cooldown_until > time.monotonic()
    assert len(p._fails) == 0
    assert p._cooldown_n == 1
    assert p.errors == 3 and p.last_error == "e3"


def test_window_expires_old_failures(monkeypatch):
    m = mgr()
    p = Proxy("http://p:1")
    clock = {"now": 1000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])

    m.mark_failure(p, "e1")
    m.mark_failure(p, "e2")
    clock["now"] += 61.0

    m.mark_failure(p, "e3")
    assert p.cooldown_until == 0.0

    m.mark_failure(p, "e4")
    m.mark_failure(p, "e5")
    assert p.cooldown_until == pytest.approx(1121.0)
    assert p._cooldown_n == 1


def test_exponential_cooldown_growth_capped():
    m = mgr(account_fail_threshold=3, account_cooldown_seconds=10.0,
            account_cooldown_max_seconds=35.0)
    p = Proxy("http://p:1")
    for expected in (10.0, 20.0, 35.0, 35.0, 35.0):
        for _ in range(3):
            m.mark_failure(p, "e")
        assert p.cooldown_until - time.monotonic() == pytest.approx(expected, abs=0.5)
        p.cooldown_until = 0.0  # simulate cooldown expiry
        p._fails.clear()
    assert p._cooldown_n == 5


# --- breaker: 429 throttling (bypasses the threshold logic) ---


def test_mark_failure_429_default_cooldown_60s(monkeypatch):
    m = mgr(account_fail_threshold=3)
    p = Proxy("http://p:1")
    clock = {"now": 1000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])

    m.mark_failure(p, "upstream HTTP 429")
    assert p.cooldown_until == pytest.approx(1060.0)
    assert p.consecutive_errors == 1 and len(p._fails) == 0
    assert p.errors == 1 and p.last_error == "upstream HTTP 429"

    clock["now"] = 1100.0
    m.mark_failure(p, "upstream HTTP 429", retry_after=7.0)
    assert p.cooldown_until == pytest.approx(1107.0)


def test_mark_failure_retry_after_overrides_429_default(monkeypatch):
    m = mgr()
    p = Proxy("http://p:1")
    clock = {"now": 2000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])

    m.mark_failure(p, "upstream HTTP 429", retry_after=13.0)
    assert p.cooldown_until == pytest.approx(2013.0)

    # cooldown never rolls back: max with the existing value
    m.mark_failure(p, "upstream HTTP 429", retry_after=2.0)
    assert p.cooldown_until == pytest.approx(2013.0)


def test_retry_after_zero_or_past_date_reuses_proxy_immediately(monkeypatch):
    """``Retry-After: 0`` and a past HTTP-date mean "no cooldown" — the proxy
    stays ready and is selected again without any wait."""
    m = mgr(proxies=["http://p:1"])
    p = m.proxies[0]
    clock = {"now": 3000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])

    # Retry-After: 0 -> cooldown_until == now; still ready for the next pick.
    m.mark_failure(p, "upstream HTTP 429", retry_after=0.0)
    assert p.cooldown_until <= clock["now"]
    assert m.next_proxy() is p

    # A past HTTP-date parses to 0.0 and likewise keeps the proxy ready.
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=30), usegmt=True)
    assert opencode.parse_retry_after(httpx.Response(429, headers={"Retry-After": past})) == 0.0
    m.mark_failure(p, "upstream HTTP 429", retry_after=0.0)
    assert p.cooldown_until <= clock["now"]
    assert m.next_proxy() is p


# --- 429 parking: 3 consecutive 429 cooldowns -> 1 hour ---


def test_three_consecutive_429_cooldowns_park_for_hour(monkeypatch):
    m = mgr(proxies=["http://p:1"])
    p = m.proxies[0]
    clock = {"now": 1000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])

    m.mark_failure(p, "upstream HTTP 429")
    assert p.cooldown_until == pytest.approx(1060.0)
    assert p._throttle_n == 1

    clock["now"] += 61.0
    m.mark_failure(p, "upstream HTTP 429")
    assert p.cooldown_until == pytest.approx(1121.0)
    assert p._throttle_n == 2

    clock["now"] += 61.0
    m.mark_failure(p, "upstream HTTP 429")
    assert p.cooldown_until == pytest.approx(1122.0 + 3600.0)  # an hour, not a minute
    assert p._throttle_n == 0  # streak reset
    assert p.url == "http://p:1"  # the member IS the proxy: nothing to release


def test_success_resets_429_streak(monkeypatch):
    m = mgr(proxies=["http://p:1"])
    p = m.proxies[0]
    clock = {"now": 1000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])

    m.mark_failure(p, "upstream HTTP 429")
    m.mark_failure(p, "upstream HTTP 429")
    m.mark_success(p)

    clock["now"] += 200.0
    m.mark_failure(p, "upstream HTTP 429")
    assert p._throttle_n == 1
    assert p.cooldown_until == pytest.approx(clock["now"] + 60.0)


def test_park_respects_larger_retry_after(monkeypatch):
    m = mgr()
    p = Proxy("http://p:1")
    clock = {"now": 5000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])

    m.mark_failure(p, "upstream HTTP 429")
    m.mark_failure(p, "upstream HTTP 429")
    clock["now"] += 61.0
    m.mark_failure(p, "upstream HTTP 429", retry_after=7200.0)
    assert p.cooldown_until == pytest.approx(5061.0 + 7200.0)


def test_mark_success_clears_cooldown_but_not_disabled():
    m = mgr()
    p = Proxy("http://p:1")
    for i in range(3):
        m.mark_failure(p, f"e{i}")
    assert p.cooldown_until > 0

    m.mark_success(p)
    assert p.cooldown_until == 0.0
    assert p.consecutive_errors == 0 and len(p._fails) == 0

    p.disabled = True
    m.mark_failure(p, "e")  # while disabled: window accumulates but no cooldown
    m.mark_success(p)
    assert p.disabled is True  # success never resurrects a disabled member


# --- next_proxy: uniform round-robin ---


def test_next_proxy_round_robin_and_exclude():
    m = mgr(proxies=["http://a:1", "http://b:1", "http://c:1"])
    assert [m.next_proxy().url for _ in range(3)] == ["http://a:1", "http://b:1", "http://c:1"]
    assert m.next_proxy(exclude={"http://a:1", "http://b:1"}).url == "http://c:1"
    with pytest.raises(NoReadyProxyError) as ei:
        m.next_proxy(exclude={"http://a:1", "http://b:1", "http://c:1"})
    assert ei.value.retry_after is None  # excluded candidates do not revive by waiting


def test_next_proxy_no_double_pick_after_exclude():
    m = mgr(proxies=["http://a:1", "http://b:1", "http://c:1"])
    assert m.next_proxy(exclude={"http://a:1"}).url == "http://b:1"
    assert m.next_proxy().url == "http://c:1"  # not b twice in a row


def test_next_proxy_uniform_rr_with_cooling_middle():
    m = mgr(proxies=["http://a:1", "http://b:1", "http://c:1"])
    m.proxies[1].cooldown_until = time.monotonic() + 100
    picks = [m.next_proxy().url for _ in range(6)]
    assert picks == ["http://a:1", "http://c:1"] * 3

    m.proxies[1].cooldown_until = 0.0
    assert [m.next_proxy().url for _ in range(3)] == ["http://a:1", "http://b:1", "http://c:1"]


def test_next_proxy_raises_when_all_cooling():
    m = mgr(proxies=["http://a:1", "http://b:1"])
    a, b = m.proxies
    now = time.monotonic()
    a.cooldown_until = now + 500
    b.cooldown_until = now + 50
    with pytest.raises(NoReadyProxyError) as ei:
        m.next_proxy()
    assert ei.value.retry_after == pytest.approx(50.0, abs=1.0)
    with pytest.raises(NoReadyProxyError):
        m.next_proxy(exclude={a.url, b.url})


def test_next_proxy_raises_when_pool_empty():
    m = mgr()
    with pytest.raises(NoReadyProxyError) as ei:
        m.next_proxy()
    assert ei.value.retry_after is None


def test_next_proxy_skips_manually_disabled():
    m = mgr(proxies=["http://a:1", "http://b:1"])
    m.set_manually_disabled("http://a:1", True)
    assert m.next_proxy().url == "http://b:1"


# --- local RPM cap ---


def test_rpm_cap_skips_until_window_slot_frees(monkeypatch):
    m = mgr(proxies=["http://a:1", "http://b:1"], openroute_rpm_limit=2)
    a, b = m.proxies
    clock = {"now": 1000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])

    for _ in range(2):
        m.record_request(a)
    assert m.status()["proxies_rpm_capped"] == 1
    assert m.next_proxy() is b

    m.record_request(b)
    assert m.next_proxy() is b
    m.record_request(b)
    with pytest.raises(NoReadyProxyError) as ei:
        m.next_proxy()
    assert ei.value.retry_after == pytest.approx(60.0)

    clock["now"] += 59.9
    with pytest.raises(NoReadyProxyError):
        m.next_proxy()

    clock["now"] += 0.2
    assert m.next_proxy() is a
    assert m.status()["proxies_rpm_capped"] == 0


def test_rpm_window_counts_only_last_60_seconds(monkeypatch):
    m = mgr(openroute_rpm_limit=40)
    p = Proxy("http://p:1")
    clock = {"now": 1000.0}
    monkeypatch.setattr(prox.time, "monotonic", lambda: clock["now"])
    for _ in range(40):
        m.record_request(p)
    assert m._rpm_capped(p, time.monotonic()) is True
    clock["now"] += 60.0
    assert m._rpm_capped(p, time.monotonic()) is False
    assert len(p._rpm) == 0
    assert p.requests == 0  # record_request never touches the total counter


def test_rpm_cap_disabled_with_zero_limit():
    m = mgr(openroute_rpm_limit=0)
    p = Proxy("http://p:1")
    for _ in range(100):
        m.record_request(p)
    assert m._rpm_capped(p, time.monotonic()) is False


# --- proxy parsing ---


def test_proxy_url_accepts_dicts_and_strings_rejects_malformed():
    pu = ProxyManager._proxy_url
    assert pu({"host": "h1", "port": 8081, "user": "u", "password": "p"}) == "http://u:p@h1:8081"
    assert pu({"host": "h1", "port": "8081"}) == "http://h1:8081"
    assert pu("http://u:p@h2:8082") == "http://u:p@h2:8082"
    assert pu("http://h3:8083") == "http://h3:8083"
    assert pu("h4:8084") == "http://h4:8084"
    assert pu({"host": "h"}) is None
    assert pu({"port": 8080}) is None
    assert pu({"host": "", "port": 1}) is None
    assert pu({"host": "h", "port": "abc"}) is None
    assert pu({"host": "h", "port": 70000}) is None
    assert pu("not a proxy") is None
    assert pu("http://h") is None
    assert pu(42) is None
    assert pu(None) is None
    desc = ProxyManager._describe_proxy_item({"host": "h", "password": "topsecret"})
    assert desc == "dict, keys=['host', 'password']"
    assert "topsecret" not in desc


@pytest.mark.asyncio
async def test_refresh_proxies_skips_malformed_and_dedups(caplog):
    caplog.set_level(logging.WARNING, logger="gateway.proxies")
    m = mgr()
    payload = {"proxies": [
        {"host": "h1.internal", "port": 8081, "user": "u1", "password": "p1"},
        {"host": "h1.internal", "port": 8081, "user": "u1", "password": "p1"},  # dup
        {"host": "h2.internal"},                       # no port -> malformed
        "http://u2:p2@h3.internal:8083",                # string with creds
        "h4.internal:8084",                            # string without scheme
        "not a proxy",                                 # space -> malformed
        42,                                            # wrong type
        {"host": "", "port": 1},                       # empty host
        {"host": "h5.internal", "port": "abc"},        # non-numeric port
        {"host": "h6.internal", "port": 70000},       # out of range
    ]}
    await _fake_fetch(m, payload)
    # patch the probe so no network is attempted
    import types
    m._probe_proxy = types.MethodType(_alive_probe, m)

    assert await m.refresh_proxies() is True
    assert [p.url for p in m.proxies] == [
        "http://u1:p1@h1.internal:8081",
        "http://u2:p2@h3.internal:8083",
        "http://h4.internal:8084",
    ]
    assert "Skipping malformed proxy item" in caplog.text
    assert m.last_fetch_status and "3 proxies" in m.last_fetch_status
    await m.aclose()


@pytest.mark.asyncio
async def test_refresh_proxies_bust_cache_adds_refresh_param_only_when_asked():
    import types

    m = mgr()
    await _fake_fetch(m, {"proxies": []})
    m._probe_proxy = types.MethodType(_alive_probe, m)

    assert await m.refresh_proxies(bust_cache=True) is True
    assert m._fetcher.url == "http://127.0.0.1:1/proxies?refresh=true"

    # The periodic loop keeps using the cached URL (no ?refresh=true).
    assert await m.refresh_proxies() is True
    assert m._fetcher.url == "http://127.0.0.1:1/proxies"
    await m.aclose()


@pytest.mark.asyncio
async def test_refresh_proxies_bad_payload_is_fetch_failure():
    m = mgr()
    await _fake_fetch(m, "garbage")
    assert await m.refresh_proxies() is False
    await m.aclose()


@pytest.mark.asyncio
async def test_refresh_failure_redacts_credentials_in_last_fetch_status():
    """A fetch/HTTP error message may embed PROXIES_URL with credentials; the
    status payload (exposed via /health and /admin/status) must be redacted."""
    m = mgr()
    url = "http://svc:topsecret@px6-service:8000/proxies"

    class _BoomFetcher:
        async def get(self, _url):
            raise httpx.HTTPStatusError(
                f"500 Server Error for url: {url}",
                request=httpx.Request("GET", url),
                response=httpx.Response(500),
            )

        async def aclose(self):
            pass

    old = m._fetcher
    m._fetcher = _BoomFetcher()
    await old.aclose()

    assert await m.refresh_proxies() is False
    assert "topsecret" not in (m.last_fetch_status or "")
    assert "http://***@px6-service:8000/proxies" in (m.last_fetch_status or "")
    # status() is the payload /health and /admin/status return
    assert "topsecret" not in json.dumps(m.status())
    assert "http://***@px6-service:8000/proxies" in m.status()["last_proxies_fetch"]
    await m.aclose()


def test_redact_url_strips_userinfo_and_secret():
    assert prox.redact_url("http://user:topsecret@10.0.0.1:3128") == "http://***@10.0.0.1:3128"
    assert prox.redact_url(
        "postgresql://openroute:openroute@db:5432/openroute"
    ) == "postgresql://***@db:5432/openroute"
    assert prox.redact_url("https://px6.link/api/SECRET/getproxy", "SECRET") == \
        "https://px6.link/api/***/getproxy"
    assert prox.redact_url("500 for url 'http://u:p@h:1/x'") == "500 for url 'http://***@h:1/x'"
    assert prox.redact_url(None) == ""


@pytest.mark.asyncio
async def test_parking_persists_across_manager_restart(pg_dsn):
    from gateway.proxydb import ProxyDB

    quota_until = time.time() + 3600
    banned_until = time.time() + 7200
    db = ProxyDB(pg_dsn)
    assert db.upsert_many(["http://h1:1"]) == 1

    m = ProxyManager(build_settings(pg_dsn))
    m.bind_db(db)
    m.apply_proxies(db.all())
    group = m.egress_groups()[0]
    m.mark_quota_exhausted(group, quota_until)
    m.mark_banned(group, banned_until)
    await m.aclose()
    db.close()

    # A fresh manager (gateway restart) must recover the parking from the DB.
    db2 = ProxyDB(pg_dsn)
    m2 = ProxyManager(build_settings(pg_dsn))
    m2.bind_db(db2)
    m2.apply_proxies(db2.all())
    m2.load_parking(db2.parking_all())
    restored = m2.egress_groups()[0]
    assert restored.quota_until == pytest.approx(quota_until)
    assert restored.banned_until == pytest.approx(banned_until)
    status = m2.status()
    assert status["egress_groups_quota_parked"] == 1
    assert status["egress_groups_banned"] == 1
    assert status["proxies"][0]["quota_seconds_left"] > 3500
    db2.close()
    await m2.aclose()


def test_permanently_banned_bucket_is_never_selected_or_waited_for():
    m = mgr(proxies=["http://u:p@a:1", "http://u2:p@b:1"])
    a, b = m.egress_groups()
    m.mark_permanently_banned(a)
    assert a.permanently_banned is True and a.is_parked(time.time())
    for _ in range(3):
        assert m.next_proxy().url == b.proxies[0].url
    with pytest.raises(NoReadyProxyError) as err:
        m.next_proxy_in_group(a)
    assert err.value.kind == "saturated" and err.value.retry_after is None
    assert m.earliest_ready_seconds() is not None  # B is still usable
    m.mark_permanently_banned(b)
    with pytest.raises(NoReadyProxyError) as err:
        m.next_proxy()
    assert err.value.kind == "saturated" and err.value.retry_after is None
    assert m.earliest_ready_seconds() is None


@pytest.mark.asyncio
async def test_permanent_ban_persists_across_manager_restart(pg_dsn):
    from gateway.proxydb import ProxyDB

    db = ProxyDB(pg_dsn)
    assert db.upsert_many(["http://h1:1", "http://h2:2"]) == 2

    m = ProxyManager(build_settings(pg_dsn))
    m.bind_db(db)
    m.apply_proxies(db.all())
    a = next(g for g in m.egress_groups() if g.host == "h1:1")
    m.mark_permanently_banned(a)
    await m.aclose()
    db.close()

    db2 = ProxyDB(pg_dsn)
    m2 = ProxyManager(build_settings(pg_dsn))
    m2.bind_db(db2)
    m2.apply_proxies(db2.all())
    m2.load_parking(db2.parking_all())
    restored = next(g for g in m2.egress_groups() if g.host == "h1:1")
    assert restored.permanently_banned is True
    assert restored.is_parked(time.time())
    assert all(m2.next_proxy().url == "http://h2:2" for _ in range(3))
    assert m2.status()["egress_groups_banned"] == 1
    db2.close()
    await m2.aclose()


def test_load_parking_merges_duplicate_keys_by_max():
    m = mgr(proxies=["http://u1:p@h:1", "http://u2:p@h:1"])
    assert len(m.egress_groups()) == 1
    m.load_parking([
        ("http://u1:p@h:1", 100.0, 0.0, False),
        ("http://u2:p@h:1", 50.0, 200.0, True),
    ])
    group = m.egress_groups()[0]
    assert group.quota_until == 100.0
    assert group.banned_until == 200.0
    assert group.permanently_banned is True


@pytest.mark.asyncio
async def test_refresh_never_probes_or_unbans_permanent_ban(pg_dsn):
    from gateway.proxydb import ProxyDB

    db = ProxyDB(pg_dsn)
    assert db.upsert_many(["http://h1:1", "http://h2:2"]) == 2
    m = ProxyManager(build_settings(pg_dsn))
    m.bind_db(db)
    m.apply_proxies(db.all())
    a = next(g for g in m.egress_groups() if g.host == "h1:1")
    m.mark_permanently_banned(a)

    probed: list[str] = []

    async def probe(url: str) -> bool:
        probed.append(url)
        return True

    m._probe_proxy = probe
    await _fake_fetch(m, {"proxies": [{"host": "h1", "port": 1},
                                     {"host": "h2", "port": 2}]})
    assert await m.refresh_proxies() is True
    assert probed == ["http://h2:2"]  # banned bucket is never health-checked
    restored = next(g for g in m.egress_groups() if g.host == "h1:1")
    assert restored.permanently_banned is True
    m.mark_success(restored.proxies[0])
    assert restored.permanently_banned is True
    db.close()
    await m.aclose()


@pytest.mark.asyncio
async def test_refresh_upsert_preserves_manual_disable(pg_dsn):
    from gateway.proxydb import ProxyDB

    db = ProxyDB(pg_dsn)
    assert db.upsert_many(["http://h1:1"]) == 1
    assert db.set_enabled("http://h1:1", False) is True
    m = ProxyManager(build_settings(pg_dsn))
    m.bind_db(db)
    await _fake_fetch(m, {"proxies": [{"host": "h1", "port": 1}, {"host": "h2", "port": 2}]})
    import types
    m._probe_proxy = types.MethodType(_alive_probe, m)

    assert await m.refresh_proxies() is True
    # h1 exists and keeps its disabled flag; h2 is inserted enabled
    by_url = {p.url: p for p in m.proxies}
    assert by_url["http://h1:1"].manually_disabled is True
    assert by_url["http://h2:2"].manually_disabled is False
    db.close()
    await m.aclose()


# --- probe: 407/5xx = dead ---


@pytest.mark.asyncio
async def test_probe_proxy_407_5xx_and_network_errors_mean_dead(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="gateway.proxies")
    m = mgr()
    made = {"status": 200}

    class ProbeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if made["status"] == "raise":
                raise httpx.ConnectError("refused")
            return _StatusResp(made["status"])

    monkeypatch.setattr(prox.httpx, "AsyncClient", ProbeClient)

    made["status"] = 200
    assert await m._probe_proxy("http://p:1") is True
    made["status"] = 429  # throttling THROUGH the proxy = the proxy channel is alive
    assert await m._probe_proxy("http://p:1") is True
    made["status"] = 407
    assert await m._probe_proxy("http://p:1") is False
    made["status"] = 503
    assert await m._probe_proxy("http://p:1") is False
    made["status"] = "raise"
    assert await m._probe_proxy("http://p:1") is False
    assert "dead: HTTP 407" in caplog.text
    assert "dead: ConnectError" in caplog.text
    await m.aclose()


# --- ensure_client / aclose ---


@pytest.mark.asyncio
async def test_ensure_client_rebuilds_and_closes_old():
    m = mgr()
    p = Proxy("http://p1:8080")
    c1 = m.ensure_client(p)
    assert p.client is c1 and p._client_proxy == "http://p1:8080"

    p._client_proxy = "http://stale:1"  # force a rebuild
    c2 = m.ensure_client(p)
    assert c2 is not c1
    await asyncio.sleep(0.1)  # background task (strong ref) closes the old client
    assert c1.is_closed is True
    await m.aclose()


@pytest.mark.asyncio
async def test_ensure_client_raises_after_aclose():
    m = mgr(proxies=["http://p:1"])
    p = m.proxies[0]
    m.ensure_client(p)
    await m.aclose()
    with pytest.raises(RuntimeError):
        m.ensure_client(p)


@pytest.mark.asyncio
async def test_aclose_is_idempotent():
    m = mgr(proxies=["http://p:1"])
    p = m.proxies[0]
    m.ensure_client(p)
    await m.aclose()
    await m.aclose()
    assert p.client is None


# --- status / counters / masking ---


def test_status_separated_counters_and_rpm():
    m = mgr(proxies=["http://a:1", "http://b:1", "http://c:1", "http://d:1"],
            openroute_rpm_limit=2)
    a, b, c, d = m.proxies
    b.disabled = True
    m.set_manually_disabled("http://d:1", True)
    c.cooldown_until = time.monotonic() + 10.0
    m.record_request(a)
    m.record_request(a)

    st = m.status()
    assert st["proxies_total"] == 4
    assert st["proxies_enabled"] == 2       # a, c (b dead, d manual)
    assert st["proxies_dead"] == 1
    assert st["proxies_manually_disabled"] == 1
    assert st["proxies_cooling"] == 1       # c
    assert st["proxies_rpm_capped"] == 1    # a

    per = {x["proxy"]: x for x in st["proxies"]}
    assert per["a:1"]["rpm_window"] == 2 and per["a:1"]["rpm_capped"] is True
    assert per["c:1"]["cooldown_seconds_left"] > 0
    assert per["d:1"]["enabled"] is False


def test_status_masks_proxy_creds():
    m = mgr(proxies=["http://user:topsecret@10.0.0.1:3128"])
    blob = json.dumps(m.status())
    assert "topsecret" not in blob and "user" not in blob
    row = m.status()["proxies"][0]
    assert row["proxy"] == "10.0.0.1:3128"
    assert row["url"] == "http://10.0.0.1:3128"


def test_mask_proxy_short_and_empty():
    assert mask_proxy("http://user:pass@10.0.0.1:3128") == "10.0.0.1:3128"
    assert mask_proxy("http://h:1") == "h:1"
    assert mask_proxy("") == "***"
    assert mask_proxy(None) == "***"


# --- quota buckets (one per purchased proxy URL) ---------------------------


def test_quota_buckets_are_per_proxy_url_not_entry_ip():
    """An IPv4 entry can exit via IPv6; never merge its ports into one bucket."""
    m = mgr(proxies=[
        "http://u:p@217.29.62.211:10077",
        "http://u:p@217.29.62.211:10078",
        "http://Proxy-B.INVALID:8080",
    ])
    groups = m.egress_groups()
    assert [g.host for g in groups] == [
        "217.29.62.211:10077", "217.29.62.211:10078", "proxy-b.invalid:8080"]
    assert all(len(g.proxies) == 1 for g in groups)
    assert m.group_of("http://217.29.62.211:10078") is groups[1]
    assert m.group_of("http://217.29.62.211:10078") is not groups[0]
    assert m.group_of("http://other:1") is None


def test_quota_buckets_do_not_merge_ipv6_prefixes():
    m = mgr(proxies=[
        "http://[2001:db8:0:1::1]:8080",
        "http://[2001:db8:0:1::2]:8080",
        "http://[2001:db8:0:2::1]:8080",
    ])
    groups = m.egress_groups()
    assert len(groups) == 3
    assert [g.host for g in groups] == [
        "[2001:db8:0:1::1]:8080", "[2001:db8:0:1::2]:8080",
        "[2001:db8:0:2::1]:8080"]
    assert all(len(g.proxies) == 1 for g in groups)
    assert m.group_of("http://[2001:db8:0:1::2]:8080") is groups[1]


def test_next_proxy_never_returns_parked_group_url():
    m = mgr(proxies=["http://a:1", "http://b:1"])
    a, b = m.egress_groups()
    m.mark_quota_exhausted(a, time.time() + 3600)
    for _ in range(10):
        assert m.next_proxy().url == "http://b:1"

    m.mark_banned(b, time.time() + 10)
    with pytest.raises(NoReadyProxyError) as ei:
        m.next_proxy()
    assert ei.value.kind == "saturated"
    assert ei.value.retry_after == pytest.approx(10.0, abs=2.0)

    a.quota_until = time.time() - 1
    assert m.next_proxy().url == "http://a:1"


def test_mark_success_never_clears_group_parking():
    m = mgr(proxies=["http://a:1", "http://b:1"])
    a = m.egress_groups()[0]
    m.mark_quota_exhausted(a, time.time() + 100)
    m.mark_banned(a, time.time() + 200)
    p = a.proxies[0]
    m.mark_success(p)
    assert a.quota_until > time.time() and a.banned_until > time.time()
    assert m.next_proxy().url == "http://b:1"  # a stays parked


def test_apply_proxies_preserves_parking_per_proxy_on_refresh():
    m = mgr(proxies=["http://10.0.0.1:1", "http://10.0.0.2:2"])
    g1, g2 = m.egress_groups()
    until = time.time() + 500
    m.mark_quota_exhausted(g1, until)
    m.mark_banned(g2, until)

    # Refresh: same rows + a new port on the same entry IP. The new port must
    # NOT inherit the parked bucket of :1 (IPv4 entry != IPv6 exit).
    m.apply_proxies([
        ("http://10.0.0.1:1", True),
        ("http://10.0.0.1:3", True),
        ("http://10.0.0.2:2", True),
    ])
    groups = m.egress_groups()
    assert [g.host for g in groups] == [
        "10.0.0.1:1", "10.0.0.1:3", "10.0.0.2:2"]
    assert [len(g.proxies) for g in groups] == [1, 1, 1]
    assert groups[0].quota_until == until
    assert groups[1].quota_until == 0.0
    assert groups[2].banned_until == until


def test_earliest_ready_seconds_includes_parked_group_even_if_excluded():
    m = mgr(proxies=["http://a:1", "http://b:1"])
    a, b = m.egress_groups()
    m.mark_quota_exhausted(a, time.time() + 120)
    b.proxies[0].cooldown_until = time.monotonic() + 5
    excluded = {p.url for p in m.proxies}
    # b is excluded and therefore not a wait candidate; parked a still is.
    assert m.earliest_ready_seconds(excluded) == pytest.approx(120.0, abs=2.0)
    with pytest.raises(NoReadyProxyError) as ei:
        m.next_proxy(exclude=excluded)
    assert ei.value.kind == "saturated"
    assert ei.value.retry_after == pytest.approx(120.0, abs=2.0)


def test_no_ready_kind_empty_excluded_saturated():
    empty = mgr()
    with pytest.raises(NoReadyProxyError) as ei:
        empty.next_proxy()
    assert (ei.value.kind, ei.value.retry_after) == ("empty", None)

    m = mgr(proxies=["http://a:1", "http://b:1"])
    with pytest.raises(NoReadyProxyError) as ei:
        m.next_proxy(exclude={p.url for p in m.proxies})
    assert (ei.value.kind, ei.value.retry_after) == ("excluded", None)

    for p in m.proxies:
        p.cooldown_until = time.monotonic() + 30
    with pytest.raises(NoReadyProxyError) as ei:
        m.next_proxy()
    assert ei.value.kind == "saturated"
    assert ei.value.retry_after == pytest.approx(30.0, abs=2.0)


def test_next_proxy_in_group_returns_its_only_proxy():
    m = mgr(proxies=["http://10.0.0.1:1"])
    g = m.egress_groups()[0]
    assert m.next_proxy_in_group(g).url == "http://10.0.0.1:1"
    with pytest.raises(NoReadyProxyError):
        m.next_proxy_in_group(g, exclude={"http://10.0.0.1:1"})


@pytest.mark.asyncio
async def test_refresh_skips_probing_parked_groups():
    m = mgr(proxies=["http://a:1", "http://b:1"])
    a = m.egress_groups()[0]
    m.mark_quota_exhausted(a, time.time() + 3600)
    probed: list[str] = []

    async def record_probe(url):
        probed.append(url)
        return True

    import types
    m._probe_proxy = types.MethodType(lambda self, url: record_probe(url), m)
    await _fake_fetch(m, {"proxies": [{"host": "a", "port": 1},
                                     {"host": "b", "port": 1}]})
    assert await m.refresh_proxies() is True
    assert probed == ["http://b:1"]
    assert a.quota_until > time.time()
    await m.aclose()


def test_status_adds_group_info_without_breaking_proxy_order():
    m = mgr(proxies=["http://10.0.0.1:1", "http://10.0.0.1:2", "http://10.0.0.2:3"])
    st = m.status()
    assert [row["proxy"] for row in st["proxies"]] == [
        "10.0.0.1:1", "10.0.0.1:2", "10.0.0.2:3"]
    assert st["egress_groups_total"] == 3
    assert st["egress_groups_ready"] == 3
    assert st["egress_groups_quota_parked"] == 0
    assert st["egress_groups_banned"] == 0
    assert [g["ip"] for g in st["egress_groups"]] == [
        "10.0.0.1:1", "10.0.0.1:2", "10.0.0.2:3"]
    assert all(g["proxy_count"] == 1 for g in st["egress_groups"])


def test_status_exposes_per_proxy_quota_and_ready_count():
    m = mgr(proxies=["http://10.0.0.1:1", "http://10.0.0.2:2"])
    g = m.egress_groups()[0]
    m.mark_quota_exhausted(g, time.time() + 3600)
    st = m.status()
    parked, healthy = st["proxies"]
    assert parked["egress_group"] == "10.0.0.1:1"
    assert parked["quota_seconds_left"] > 3500
    assert healthy["quota_seconds_left"] == 0.0
    assert st["proxies_ready"] == 1
    assert st["egress_groups_quota_parked"] == 1

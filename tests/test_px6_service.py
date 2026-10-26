"""px6-service endpoint tests (account preview, catalog, purchase).

The service package is loaded by path because the directory name contains a
hyphen. px6.net itself is replaced with an in-memory fake: no network, no API
key.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

from fastapi.testclient import TestClient

TESTS_DIR = pathlib.Path(__file__).resolve().parent
PX6_DIR = TESTS_DIR.parent / "px6-service"

# app/config.py requires the key at import time (the real px6.net client is
# never called in these tests).
os.environ.setdefault("PX6_API_KEY", "px6-test-key")


def _load_package(name: str, pkg_dir: pathlib.Path):
    spec = importlib.util.spec_from_file_location(
        name, str(pkg_dir / "__init__.py"),
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package("px6service", PX6_DIR / "app")
_main_spec = importlib.util.spec_from_file_location(
    "px6service.main", str(PX6_DIR / "app" / "main.py"),
)
px6main = importlib.util.module_from_spec(_main_spec)
sys.modules["px6service.main"] = px6main
_main_spec.loader.exec_module(px6main)


def _item(index: int, descr: str) -> dict:
    return {
        "id": str(index),
        "version": "6",
        "ip": f"10.0.0.{index}",
        "host": f"10.0.0.{index}",
        "port": "8080",
        "user": "u",
        "pass": "p",
        "type": "http",
        "country": "ru",
        "date": "2026-01-01 00:00:00",
        "date_end": "2026-12-01 00:00:00",
        "unixtime": 1,
        "unixtime_end": 2,
        "descr": descr,
        "active": "1",
    }


class FakePx6:
    def __init__(self) -> None:
        self.proxies = [_item(1, "OPENROUTE"), _item(2, "OTHER")]
        self.proxy_calls: list[tuple] = []
        self.buy_calls: list[dict] = []
        self.closed = False

    async def get_proxy(self, descr=None, state="active"):
        self.proxy_calls.append((descr, state))
        return {"list": {str(index): item for index, item in enumerate(self.proxies)}}

    async def account(self):
        return {"user_id": "u1", "balance": "100.50", "currency": "RUB"}

    async def get_countries(self, version):
        return {"list": ["ru", "de", "RU"]}

    async def get_price(self, version, count, period):
        return {
            "user_id": "u1", "balance": "100.50", "currency": "RUB",
            "price": "25.00", "price_single": "12.50",
        }

    async def buy(self, **kwargs):
        self.buy_calls.append(kwargs)
        self.proxies.append(_item(3, "BUY"))
        return {
            "order_id": "o1", "count": kwargs["count"], "price": "25.00",
            "price_single": "12.50", "period": kwargs["period"],
            "country": kwargs["country"], "currency": "RUB", "balance": "75.50",
        }

    async def aclose(self):
        self.closed = True


def _client(monkeypatch) -> tuple[TestClient, FakePx6]:
    fake = FakePx6()
    monkeypatch.setattr(px6main, "px6", fake)
    monkeypatch.setattr(px6main, "cache", px6main.TTLCache(ttl_seconds=3600))
    return TestClient(px6main.app), fake


def test_proxies_returns_every_active_proxy_without_tag_filter(monkeypatch):
    client, fake = _client(monkeypatch)
    response = client.get("/proxies")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [item["descr"] for item in body["proxies"]] == ["OPENROUTE", "OTHER"]
    assert fake.proxy_calls == [(None, "active")]
    # Credentials are required by the gateway and stay inside the service
    # boundary (web-admin never receives this payload).
    assert body["proxies"][0]["password"] == "p"


def test_account_countries_and_price(monkeypatch):
    client, _ = _client(monkeypatch)
    account = client.get("/account")
    assert account.status_code == 200
    assert account.json() == {"user_id": "u1", "balance": 100.5, "currency": "RUB"}

    countries = client.get("/countries?version=6")
    assert countries.status_code == 200
    assert countries.json() == {"version": 6, "countries": ["de", "ru"]}

    price = client.get("/price?version=6&count=2&period=30")
    assert price.status_code == 200
    assert price.json()["price"] == 25.0
    assert price.json()["price_single"] == 12.5

    bad = client.get("/countries?version=5")
    assert bad.status_code == 400


def test_buy_invalidates_proxy_cache_and_normalizes_request(monkeypatch):
    client, fake = _client(monkeypatch)
    assert client.get("/proxies").json()["count"] == 2

    response = client.post("/buy", json={
        "count": 1, "period": 30, "country": "RU", "version": 6,
        "descr": "prod",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == "o1"
    assert body["country"] == "ru"
    assert body["price"] == 25.0
    assert fake.buy_calls == [{
        "count": 1, "period": 30, "country": "ru", "version": 6,
        "descr": "prod", "auto_prolong": False,
    }]

    # The cache was dropped by /buy: the next /proxies request sees the new item
    # without ?refresh=true.
    assert client.get("/proxies").json()["count"] == 3
    assert fake.proxy_calls[-1] == (None, "active")


def test_buy_validation_errors(monkeypatch):
    client, fake = _client(monkeypatch)
    response = client.post("/buy", json={
        "count": 0, "period": 30, "country": "ru", "version": 6,
    })
    assert response.status_code == 422
    assert fake.buy_calls == []

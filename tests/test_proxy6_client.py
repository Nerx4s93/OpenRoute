"""px6-service unit tests: the px6 API key must never reach the logs.

The key is part of the request URL *path* (``/{key}/{method}``), a shape httpx
logs verbatim at INFO. ``app/client.py`` raises the httpx/httpcore loggers to
WARNING; these tests assert the key stays out of captured output for a failed
upstream call and that ``redact_url`` strips credentials.

``px6-service`` is not an importable package name (hyphen), so the module is
loaded from its file path. It has no relative imports of its own.
"""

from __future__ import annotations

import importlib.util
import logging
import pathlib
import sys

import httpx
import pytest

_CLIENT_PATH = (pathlib.Path(__file__).resolve().parent.parent
                / "px6-service" / "app" / "client.py")

API_KEY = "px6-test-key-DO-NOT-LEAK"


def _load_client():
    """Import ``px6-service/app/client.py`` by path (executes its module code)."""
    spec = importlib.util.spec_from_file_location("openroute_px6_client", _CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_api_key_never_appears_in_logs(caplog):
    """A failed upstream call (HTTP 500) must not log the keyed px6 URL."""
    mod = _load_client()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(500, text="upstream boom", request=request)

    client = mod.Px6Client(api_key=API_KEY, api_base="https://px6.link/api")
    await client._client.aclose()  # swap in a hermetic transport
    client._client = httpx.AsyncClient(base_url="https://px6.link/api",
                                       transport=httpx.MockTransport(handler))

    with caplog.at_level(logging.INFO):
        with pytest.raises(mod.Px6Error):
            await client._get("getproxy", {"state": "active"})
    await client.aclose()

    # the key IS in the request URL (px6 requires it in the path)...
    assert API_KEY in seen["url"]
    # ...but it must not be in any captured log output.
    assert API_KEY not in caplog.text


@pytest.mark.asyncio
async def test_account_catalog_and_buy_requests():
    mod = _load_client()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/buy"):
            return httpx.Response(200, json={
                "status": "yes", "order_id": "o1", "count": "2",
                "price": "25.00", "price_single": "12.50",
                "period": "30", "country": "ru", "currency": "RUB",
                "balance": "75.00",
            }, request=request)
        if path.endswith("/getcountry"):
            return httpx.Response(200, json={
                "status": "yes", "list": ["ru", "de"],
            }, request=request)
        if path.endswith("/getprice"):
            return httpx.Response(200, json={
                "status": "yes", "price": "12.50", "price_single": "12.50",
                "currency": "RUB", "balance": "100.00", "user_id": "u1",
            }, request=request)
        return httpx.Response(200, json={
            "status": "yes", "user_id": "u1", "balance": "100.00",
            "currency": "RUB",
        }, request=request)

    client = mod.Px6Client(api_key=API_KEY, api_base="https://px6.link/api")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://px6.link/api",
                                       transport=httpx.MockTransport(handler))

    account = await client.account()
    countries = await client.get_countries(4)
    price = await client.get_price(6, 2, 30)
    bought = await client.buy(
        count=2, period=30, country="ru", version=6, descr="prod",
    )
    await client.aclose()

    assert account["balance"] == "100.00"
    assert countries["list"] == ["ru", "de"]
    assert price["price"] == "12.50"
    assert bought["order_id"] == "o1"
    assert seen[0].url.path == f"/api/{API_KEY}"
    assert seen[1].url.params["version"] == "4"
    assert seen[2].url.params["count"] == "2"
    assert seen[2].url.params["period"] == "30"
    assert seen[3].url.params["country"] == "ru"
    assert seen[3].url.params["descr"] == "prod"


@pytest.mark.asyncio
async def test_px6_error_ids_map_to_stable_statuses():
    mod = _load_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "no", "error_id": 400, "error": "no money",
        }, request=request)

    client = mod.Px6Client(api_key=API_KEY, api_base="https://px6.link/api")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://px6.link/api",
                                       transport=httpx.MockTransport(handler))
    with pytest.raises(mod.Px6Error) as exc:
        await client.account()
    await client.aclose()

    assert exc.value.status_code == 402
    assert str(exc.value) == "Insufficient px6.net balance"
    assert "no money" not in str(exc.value)


@pytest.mark.asyncio
async def test_px6_rate_limit_is_429():
    mod = _load_client()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, request=request)

    client = mod.Px6Client(api_key=API_KEY, api_base="https://px6.link/api")
    await client._client.aclose()
    client._client = httpx.AsyncClient(base_url="https://px6.link/api",
                                       transport=httpx.MockTransport(handler))
    with pytest.raises(mod.Px6Error) as exc:
        await client.get_proxy()
    await client.aclose()

    assert exc.value.status_code == 429
    assert "rate limit" in str(exc.value).lower()


def test_redact_url_strips_userinfo_and_secret():
    mod = _load_client()
    assert mod.redact_url("http://user:topsecret@10.0.0.1:3128") == "http://***@10.0.0.1:3128"
    assert mod.redact_url("https://px6.link/api/SECRET/getproxy", "SECRET") == \
        "https://px6.link/api/***/getproxy"
    assert mod.redact_url("500 for url 'http://u:p@h:1/x'") == "500 for url 'http://***@h:1/x'"
    assert mod.redact_url(None) == ""

"""px6-service — microservice exposing the px6.net proxy account and catalog.

The service is the only component that knows the px6.net API key. It returns
**all active proxies** (no comment/tag filter); the OpenRoute gateway owns the
enabled/disabled state of its pool.

Responses for ``/proxies`` are TTL-cached (``CACHE_HOURS``); ``?refresh=true``
bypasses the cache. Purchases invalidate that cache immediately.

Endpoints:
    GET  /proxies?refresh=      -> {"count": N, "proxies": [...]}
    GET  /account               -> balance/currency of the px6.net account
    GET  /countries?version=    -> available countries for a proxy version
    GET  /price?version=&count=&period= -> price estimate
    POST /buy                   -> buy proxies (does not return credentials)
    GET  /health                -> {"status": "ok", "cached": bool}
"""

import logging
from contextlib import asynccontextmanager
from typing import NoReturn

from fastapi import FastAPI, HTTPException, Query

from .cache import TTLCache
from .client import Px6Client, Px6Error, redact_url
from .config import get_settings
from .schemas import (
    AccountResponse,
    BuyRequest,
    BuyResponse,
    CountriesResponse,
    PriceResponse,
    ProxyItem,
    ProxyListResponse,
    StatusResponse,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# The px6 API key is part of the request URL path; httpx/httpcore must not log it.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

settings = get_settings()
px6 = Px6Client(api_key=settings.px6_api_key, api_base=settings.px6_api_base)
cache = TTLCache(ttl_seconds=settings.cache_hours * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Service started, cache TTL: %sh", settings.cache_hours)
    yield
    await px6.aclose()


app = FastAPI(
    title="px6-service",
    description="Микросервис-обёртка над px6.net: баланс, каталог и покупка прокси",
    version="2.0.0",
    lifespan=lifespan,
)


def _raise_px6(exc: Px6Error) -> NoReturn:
    """Re-raise a px6 error as an HTTP error with the API key redacted."""
    raise HTTPException(
        status_code=exc.status_code,
        detail=redact_url(str(exc), settings.px6_api_key),
    ) from exc


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_list(raw: object) -> list:
    if isinstance(raw, dict):
        return list(raw.values())
    return list(raw) if isinstance(raw, (list, tuple)) else []


async def _fetch_proxies() -> list[ProxyItem]:
    """Fetch every active proxy; no ``descr``/tag filter is applied."""
    data = await px6.get_proxy(state="active")
    raw_proxies = _as_list(data.get("list") or {})

    proxies: list[ProxyItem] = []
    for p in raw_proxies:
        if not isinstance(p, dict):
            continue
        try:
            proxies.append(
                ProxyItem(
                    id=str(p["id"]),
                    version=str(p["version"]),
                    ip=str(p["ip"]),
                    host=str(p["host"]),
                    port=str(p["port"]),
                    user=p.get("user"),
                    password=p.get("pass"),
                    type=str(p["type"]),
                    country=str(p["country"]),
                    date=str(p["date"]),
                    date_end=str(p["date_end"]),
                    unixtime=int(p["unixtime"]),
                    unixtime_end=int(p["unixtime_end"]),
                    descr=str(p.get("descr") or ""),
                    active=str(p.get("active") or ""),
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed px6.net proxy item")
    return proxies


@app.get("/proxies", response_model=ProxyListResponse,
         summary="Список всех активных прокси px6.net")
async def get_proxies(refresh: bool = Query(False, description="Принудительно обновить кэш")) -> ProxyListResponse:
    if refresh:
        cache.invalidate()

    try:
        proxies = await cache.get_or_set(_fetch_proxies)
    except Px6Error as exc:
        _raise_px6(exc)

    logger.info("Returning %d proxies (refresh=%s)", len(proxies), refresh)
    return ProxyListResponse(count=len(proxies), proxies=proxies)


@app.get("/account", response_model=AccountResponse,
         summary="Баланс аккаунта px6.net")
async def get_account() -> AccountResponse:
    try:
        data = await px6.account()
    except Px6Error as exc:
        _raise_px6(exc)
    return AccountResponse(
        user_id=str(data.get("user_id") or ""),
        balance=_as_float(data.get("balance")),
        currency=str(data.get("currency") or "RUB"),
    )


def _check_version(version: int) -> int:
    if version not in (3, 4, 6):
        raise HTTPException(
            status_code=400,
            detail="Unsupported proxy version (allowed: 3, 4, 6)",
        )
    return version


@app.get("/countries", response_model=CountriesResponse,
         summary="Доступные страны для версии прокси")
async def get_countries(version: int = Query(6, description="3=IPv4 Shared, 4=IPv4, 6=IPv6")) -> CountriesResponse:
    _check_version(version)
    try:
        data = await px6.get_countries(version)
    except Px6Error as exc:
        _raise_px6(exc)
    countries = sorted(
        {
            str(item).strip().lower()
            for item in _as_list(data.get("list") or [])
            if str(item).strip()
        }
    )
    return CountriesResponse(version=version, countries=countries)


@app.get("/price", response_model=PriceResponse,
         summary="Стоимость прокси по параметрам")
async def get_price(
    version: int = Query(6, description="3=IPv4 Shared, 4=IPv4, 6=IPv6"),
    count: int = Query(1, ge=1, le=1000),
    period: int = Query(30, ge=1, le=365),
) -> PriceResponse:
    _check_version(version)
    try:
        data = await px6.get_price(version, count, period)
    except Px6Error as exc:
        _raise_px6(exc)
    price = _as_float(data.get("price"))
    return PriceResponse(
        user_id=str(data.get("user_id") or ""),
        balance=_as_float(data.get("balance")),
        currency=str(data.get("currency") or "RUB"),
        price=price,
        price_single=_as_float(data.get("price_single"), price / count if count else 0.0),
        period=period,
        count=count,
        version=version,
    )


@app.post("/buy", response_model=BuyResponse,
          summary="Купить прокси (учётные данные не возвращаются)")
async def buy(body: BuyRequest) -> BuyResponse:
    try:
        data = await px6.buy(
            count=body.count,
            period=body.period,
            country=body.country,
            version=body.version,
            descr=body.descr,
            auto_prolong=body.auto_prolong,
        )
    except Px6Error as exc:
        _raise_px6(exc)

    # A purchase changes the account balance and the active proxy list. The
    # gateway calls /proxies?refresh=true right after this response, so dropping
    # the cache here makes the new proxies visible immediately.
    cache.invalidate()

    price = _as_float(data.get("price"))
    price_single = _as_float(
        data.get("price_single"), price / body.count if body.count else 0.0
    )
    logger.info(
        "Purchased %d proxies: version=%s country=%s period=%s order=%s",
        body.count, body.version, body.country, body.period, data.get("order_id"),
    )
    return BuyResponse(
        order_id=str(data.get("order_id") or ""),
        count=body.count,
        price=price,
        price_single=price_single,
        period=body.period,
        country=body.country,
        version=body.version,
        currency=str(data.get("currency") or "RUB"),
        balance=_as_float(data.get("balance")),
    )


@app.get("/health", response_model=StatusResponse, summary="Проверка живости")
async def health() -> StatusResponse:
    return StatusResponse(status="ok", cached=cache.get() is not None)

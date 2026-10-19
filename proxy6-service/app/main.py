"""proxy6-service — microservice exposing the px6.net proxy list filtered by
``descr == "OPENROUTE"``.

Only proxies tagged with the OPENROUTE comment in the px6.net panel are
returned (the exact analogue of NVRoute's ``USE_IN_API`` flag). Responses are
TTL-cached (``CACHE_HOURS``); ``?refresh=true`` bypasses the cache.

Endpoints:
    GET /proxies?refresh=  -> {"count": N, "proxies": [...]}
    GET /health            -> {"status": "ok", "cached": bool}
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from .cache import TTLCache
from .client import Px6Client, Px6Error, redact_url
from .config import get_settings
from .schemas import ProxyItem, ProxyListResponse, StatusResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# The px6 API key is part of the request URL path; httpx/httpcore must not log it.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

#: Only proxies whose px6.net ``descr`` equals this flag enter the pool.
TARGET_DESCR = "OPENROUTE"

settings = get_settings()
px6 = Px6Client(api_key=settings.px6_api_key, api_base=settings.px6_api_base)
cache = TTLCache(ttl_seconds=settings.cache_hours * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Service started, cache TTL: %sh, descr=%s", settings.cache_hours, TARGET_DESCR)
    yield
    await px6.aclose()


app = FastAPI(
    title="proxy6-service",
    description=f"Микросервис-обёртка над px6.net: отдаёт только прокси с descr == {TARGET_DESCR}",
    version="1.0.0",
    lifespan=lifespan,
)


async def _fetch_proxies() -> list[ProxyItem]:
    data = await px6.get_proxy(descr=TARGET_DESCR, state="active")
    raw_list = data.get("list") or {}
    if isinstance(raw_list, dict):
        raw_proxies = list(raw_list.values())
    else:
        raw_proxies = raw_list

    proxies = [
        ProxyItem(
            id=p["id"],
            version=p["version"],
            ip=p["ip"],
            host=p["host"],
            port=p["port"],
            user=p.get("user"),
            password=p.get("pass"),
            type=p["type"],
            country=p["country"],
            date=p["date"],
            date_end=p["date_end"],
            unixtime=p["unixtime"],
            unixtime_end=p["unixtime_end"],
            descr=p.get("descr", ""),
            active=p.get("active", ""),
        )
        for p in raw_proxies
        if p.get("descr") == TARGET_DESCR
    ]
    return proxies


@app.get("/proxies", response_model=ProxyListResponse,
         summary=f"Список прокси с descr == {TARGET_DESCR}")
async def get_proxies(refresh: bool = Query(False, description="Принудительно обновить кэш")) -> ProxyListResponse:
    if refresh:
        cache.invalidate()

    try:
        proxies = await cache.get_or_set(_fetch_proxies)
    except Px6Error as exc:
        raise HTTPException(status_code=exc.status_code,
                            detail=redact_url(str(exc), settings.px6_api_key)) from exc

    logger.info("Returning %d proxies (cache_hit=%s)", len(proxies), not refresh)
    return ProxyListResponse(count=len(proxies), proxies=proxies)


@app.get("/health", response_model=StatusResponse, summary="Проверка живости")
async def health() -> StatusResponse:
    return StatusResponse(status="ok", cached=cache.get() is not None)

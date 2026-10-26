import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# The px6 API key travels in the request URL *path* (``/{key}/{method}``), a
# shape httpx/httpcore log verbatim at INFO. Keep their loggers above INFO so
# the keyed URL can never reach the service logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

#: ``scheme://user:pass@`` -> ``scheme://***@`` inside arbitrary log text.
_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s@]*@")


def redact_url(value: str | None, secret: str | None = None) -> str:
    """Strip credentials from a URL (or any text containing one) for logs.

    Removes ``user:pass@`` userinfo (proxy/DSN credentials) and, when given,
    every occurrence of ``secret`` — the px6 API key sits in the URL path.
    """
    out = value or ""
    if secret:
        out = out.replace(secret, "***")
    return _USERINFO_RE.sub(r"\1***@", out)


class Px6Error(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(detail)


# Stable messages/statuses for px6.net error IDs (see the API docs). The web
# admin localizes these strings; raw upstream text is only logged (redacted).
_ERROR_STATUS = {
    30: 502,
    100: 502,
    105: 502,
    110: 502,
    200: 400,
    210: 400,
    220: 400,
    230: 400,
    240: 400,
    250: 400,
    260: 400,
    270: 400,
    280: 400,
    300: 409,
    400: 402,
    404: 404,
    410: 400,
}
_ERROR_MESSAGES = {
    30: "Unknown px6.net error",
    100: "Invalid px6.net API key",
    105: "px6.net API IP restriction",
    110: "Invalid px6.net API method",
    200: "Invalid proxy count",
    210: "Invalid proxy period",
    220: "Invalid country code",
    230: "Invalid proxy IDs",
    240: "Invalid proxy version",
    250: "Invalid proxy comment",
    260: "Invalid proxy type",
    270: "Invalid proxy port",
    280: "Invalid proxy string",
    300: "Not enough proxies available",
    400: "Insufficient px6.net balance",
    404: "Proxy not found on px6.net",
    410: "Invalid price",
}


class Px6Client:
    """Thin wrapper around the px6.net HTTP API."""

    def __init__(self, api_key: str, api_base: str = "https://px6.link/api", timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._api_base,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # An empty method is the account endpoint: /{key} (balance + currency).
        url = f"/{self._api_key}" + (f"/{method}" if method else "")
        try:
            resp = await self._client.get(url, params=params or {})
        except httpx.HTTPError as exc:
            logger.error("px6.net request failed: %s", redact_url(str(exc), self._api_key))
            raise Px6Error(502, "Upstream request to px6.net failed") from exc

        if resp.status_code == 429:
            raise Px6Error(429, "Upstream rate limit (px6.net): too many requests")
        if resp.status_code != 200:
            raise Px6Error(502, f"Upstream returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise Px6Error(502, "Upstream returned non-JSON response") from exc

        if data.get("status") != "yes":
            try:
                error_id = int(data.get("error_id") or 30)
            except (TypeError, ValueError):
                error_id = 30
            error = data.get("error", "unknown upstream error")
            logger.error("px6.net API error %s: %s (method=%s)",
                         error_id, redact_url(str(error), self._api_key),
                         method or "account")
            raise Px6Error(
                _ERROR_STATUS.get(error_id, 502),
                _ERROR_MESSAGES.get(error_id, "px6.net request failed"),
            )

        return data

    async def account(self) -> dict[str, Any]:
        """Balance/currency (the API root with just the key)."""
        return await self._get("")

    async def get_proxy(self, descr: str | None = None, state: str = "active") -> dict[str, Any]:
        params: dict[str, Any] = {"state": state}
        if descr is not None:
            params["descr"] = descr
        return await self._get("getproxy", params)

    async def get_countries(self, version: int) -> dict[str, Any]:
        return await self._get("getcountry", {"version": version})

    async def get_price(self, version: int, count: int, period: int) -> dict[str, Any]:
        return await self._get(
            "getprice", {"version": version, "count": count, "period": period}
        )

    async def buy(
        self,
        *,
        count: int,
        period: int,
        country: str,
        version: int,
        descr: str = "",
        auto_prolong: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "count": count,
            "period": period,
            "country": country,
            "version": version,
        }
        if descr:
            params["descr"] = descr
        if auto_prolong:
            params["auto_prolong"] = "1"
        return await self._get("buy", params)

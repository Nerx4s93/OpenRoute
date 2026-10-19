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
        url = f"/{self._api_key}/{method}"
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
            error_id = data.get("error_id")
            error = data.get("error", "unknown upstream error")
            status = 500 if error_id == 100 else 502
            detail = redact_url(f"px6.net API error {error_id}: {error}", self._api_key)
            logger.error("px6.net API error %s: %s (method=%s)",
                         error_id, redact_url(str(error), self._api_key), method)
            raise Px6Error(status, detail)

        return data

    async def get_proxy(self, descr: str | None = None, state: str = "active") -> dict[str, Any]:
        params: dict[str, Any] = {"state": state}
        if descr is not None:
            params["descr"] = descr
        return await self._get("getproxy", params)

"""HTTP client for the px6-service internal API.

The gateway is the only OpenRoute component that talks to both px6-service and
the proxy pool. web-admin calls the gateway's ``/internal/px6/*`` endpoints; it
never receives px6.net credentials or the px6.net API key.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class Px6ServiceError(Exception):
    """Stable error from px6-service (status + safe human-readable detail)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class Px6ServiceClient:
    def __init__(self, settings: Settings, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.px6_service_url.rstrip("/"),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError:
            raise Px6ServiceError(502, "px6-service unavailable") from None

        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                raw = payload.get("detail")
                if isinstance(raw, str):
                    detail = raw
                elif isinstance(raw, list):
                    detail = "Invalid px6-service request"
            raise Px6ServiceError(
                response.status_code,
                detail or f"px6-service returned HTTP {response.status_code}",
            )

        try:
            data = response.json()
        except ValueError:
            raise Px6ServiceError(502, "px6-service returned non-JSON response") from None
        if not isinstance(data, dict):
            raise Px6ServiceError(502, "px6-service returned an invalid response")
        return data

    async def account(self) -> dict[str, Any]:
        return await self._request("GET", "/account")

    async def countries(self, version: int) -> dict[str, Any]:
        return await self._request("GET", "/countries", params={"version": version})

    async def price(self, version: int, count: int, period: int) -> dict[str, Any]:
        return await self._request(
            "GET", "/price", params={"version": version, "count": count, "period": period}
        )

    async def buy(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/buy", json=payload)

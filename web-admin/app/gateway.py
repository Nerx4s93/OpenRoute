"""Client for the gateway's internal API and admin snapshot normalization.

The web-admin service never touches PostgreSQL directly: it talks to
``gateway`` over ``/internal/*`` with ``X-Internal-Token``. This module owns the
HTTP client, unwraps the gateway's OpenAI-style error envelope, and converts raw
gateway responses into small, browser-friendly JSON snapshots.

Proxy credentials never reach the browser: rows are exposed with a masked
``display`` (``host:port``) and an opaque ``id`` = HMAC(full proxy URL). The
full URL is kept only in this process and resolved when an operator toggles a
row.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from .config import Settings
from .schemas import KeyAddBody

logger = logging.getLogger("openroute-web-admin.gateway")

# Credentials embedded in an URL (``scheme://user:pass@host``) must never be
# echoed back in error text or recent-request rows.
_CREDENTIALS_IN_URL = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")


def _strip_credentials(value: object) -> str:
    return _CREDENTIALS_IN_URL.sub(r"\1***@", str(value or ""))


def _localize(message: str) -> str:
    """Translate the gateway's short English messages to the panel's Russian."""
    if not message:
        return message
    exact = {
        "Proxy enabled": "Прокси включён",
        "Proxy disabled": "Прокси отключён",
        "Proxy not found": "Прокси не найден",
        "Proxies refreshed": "Список прокси обновлён",
        "Proxy refresh failed": "Не удалось обновить список прокси",
        "Key enabled": "Ключ включён",
        "Key revoked (401)": "Ключ отозван",
        "Key deleted": "Ключ удалён",
        "Key not found": "Ключ не найден",
        "Such a key already exists": "Такой ключ уже есть",
        "No key given": "Ключ не указан",
        "Key too long (max 200 chars)": "Ключ слишком длинный (макс. 200 символов)",
        # --- px6 account / purchase ---
        "Unknown px6.net error": "Неизвестная ошибка px6.net",
        "Invalid px6.net API key": "Неверный API-ключ px6.net",
        "px6.net API IP restriction": "Доступ к API px6.net ограничен по IP",
        "Invalid px6.net API method": "Неверный метод API px6.net",
        "Invalid proxy count": "Некорректное количество прокси",
        "Invalid proxy period": "Некорректный период прокси",
        "Invalid country code": "Некорректный код страны",
        "Invalid proxy IDs": "Некорректные идентификаторы прокси",
        "Invalid proxy version": "Некорректная версия прокси",
        "Invalid proxy comment": "Некорректный комментарий",
        "Invalid proxy type": "Некорректный тип прокси",
        "Invalid proxy port": "Некорректный порт прокси",
        "Invalid proxy string": "Некорректная строка прокси",
        "Not enough proxies available": "На px6.net недостаточно свободных прокси",
        "Insufficient px6.net balance": "Недостаточно средств на счёте px6.net",
        "Proxy not found on px6.net": "Прокси не найден на px6.net",
        "Invalid price": "Некорректная цена",
        "Upstream request to px6.net failed": "Запрос к px6.net не удался",
        "Upstream rate limit (px6.net): too many requests": "Превышен лимит запросов к px6.net",
        "px6-service unavailable": "Сервис px6 недоступен",
        "px6-service returned non-JSON response": "Сервис px6 вернул некорректный ответ",
        "px6-service returned an invalid response": "Сервис px6 вернул некорректный ответ",
        "Unsupported proxy version (allowed: 3, 4, 6)": "Поддерживаются версии 3, 4 и 6",
        "Count must be between 1 and 1000": "Количество должно быть от 1 до 1000",
        "Period must be between 1 and 365 days": "Период должен быть от 1 до 365 дней",
        "Country must be an ISO2 code": "Страна должна быть кодом ISO2",
        "Comment must be at most 50 characters": "Комментарий не длиннее 50 символов",
        "Invalid numeric purchase parameter": "Некорректный числовой параметр покупки",
        "auto_prolong must be a boolean": "Некорректный флаг автопродления",
        "Proxies purchased and list refreshed": "Прокси куплены, список обновлён",
        "Proxies purchased; list refresh failed":
            "Прокси куплены, но список не обновился",
    }
    if message in exact:
        return exact[message]
    match = re.fullmatch(r"Key (.+) added", message)
    if match:
        return f"Ключ {match.group(1)} добавлен"
    for field in ("RPM", "TPM"):
        prefix = f"Invalid {field}: "
        if message.startswith(prefix):
            return f"Некорректный {field}: {message[len(prefix):]}"
        if message == f"{field} cannot be negative":
            return f"{field} не может быть отрицательным"
    return message


def _gateway_error(response: httpx.Response) -> HTTPException:
    """Map a gateway error response to an ``HTTPException`` with a clean detail.

    The gateway answers ALL HTTP errors (including internal 403/404/5xx) with
    the OpenAI envelope ``{"error": {"message", "type", "code"}}``; prefer
    ``payload["error"]["message"]``; fall back to FastAPI's ``{"detail": ...}``,
    then a bare ``{"message": ...}``, then the raw body.
    """
    detail: str | None = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            detail = error["message"]
        elif isinstance(payload.get("detail"), str):
            detail = payload["detail"]
        elif isinstance(payload.get("message"), str):
            detail = payload["message"]
    if not detail:
        detail = response.text[:200]
    return HTTPException(response.status_code, _localize(_strip_credentials(detail)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _masked_display(value: object) -> str:
    """Return ``host:port`` even if the source unexpectedly contains a URL."""
    text = str(value or "").strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    return text


def _sanitize_recent(raw: object) -> list[dict]:
    """Keep recent-request rows but strip anything that could hold credentials."""
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        row["account"] = _masked_display(row.get("account")) or None
        for key in ("real_proxy", "real_url", "url"):
            row.pop(key, None)
        for key, value in list(row.items()):
            if isinstance(value, str):
                row[key] = _strip_credentials(value)
        rows.append(row)
    return rows


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _iso_from_epoch(value: object) -> str:
    """Render a gateway epoch timestamp as an ISO-8601 UTC clock label."""
    try:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return _now_iso()
    return moment.isoformat(timespec="seconds")


def _lifetime_totals(lifetime_by_proxy: dict) -> dict:
    """Aggregate lifetime tokens over ALL entries, archived proxies included."""
    tokens_total = 0
    for lifetime in lifetime_by_proxy.values():
        if isinstance(lifetime, dict):
            tokens_total += _as_int(lifetime.get("tokens"))
    return {"tokens_total": tokens_total}


def _series(raw: object, length: int) -> list[int]:
    """Normalize a gateway series to exactly ``length`` ints (pad with zero)."""
    values = raw if isinstance(raw, list) else []
    series = [_as_int(value) for value in values[:length]]
    if len(series) < length:
        series.extend([0] * (length - len(series)))
    return series


def _series_block(raw: object, length: int) -> dict:
    source = raw if isinstance(raw, dict) else {}
    return {
        "tokens": _series(source.get("tokens"), length),
        "requests": _series(source.get("requests"), length),
        "errors": _series(source.get("errors"), length),
    }


def _proxy_state_fields(row: dict) -> dict:
    """Keep only the status fields the panel actually renders."""
    return {
        "enabled": bool(row.get("enabled", True)),
        "manual": bool(row.get("manual", False)),
        "disabled": bool(row.get("disabled", False)),
        "banned": bool(row.get("banned") or row.get("permanently_banned")),
        "quota_seconds_left": _as_float(row.get("quota_seconds_left")),
        "banned_seconds_left": _as_float(row.get("banned_seconds_left")),
        "cooldown_seconds_left": _as_float(row.get("cooldown_seconds_left")),
        "rpm_capped": bool(row.get("rpm_capped", False)),
        "last_error": _strip_credentials(row.get("last_error")) or None,
    }


def _px6_meta(raw: object) -> dict:
    """Non-secret px6 catalog fields (credentials never live here)."""
    source = raw if isinstance(raw, dict) else {}
    def text(key: str) -> str:
        return str(source.get(key) or "")
    return {
        "id": text("id"),
        "version": text("version"),
        "country": text("country"),
        "date_end": text("date_end"),
        "descr": _strip_credentials(text("descr")),
    }


class GatewayClient:
    """Async client for ``/internal/*`` plus snapshot normalization."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            base_url=settings.gateway_internal_url,
            headers={"X-Internal-Token": settings.internal_api_token},
            timeout=settings.internal_timeout_seconds,
        )
        # proxy_id -> full URL (credentials stay in server memory only)
        self._proxy_urls: dict[str, str] = {}

    async def aclose(self) -> None:
        await self.client.aclose()

    # --- low-level calls ---------------------------------------------------

    async def _get(self, path: str) -> dict:
        try:
            response = await self.client.get(path)
        except httpx.HTTPError as exc:
            logger.error("gateway internal API unreachable: %s", exc)
            raise HTTPException(502, "Внутренний API шлюза недоступен") from exc
        if response.status_code >= 400:
            raise _gateway_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(502, "Шлюз вернул некорректный JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(502, "Шлюз вернул неожиданный ответ")
        return payload

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            response = await self.client.post(path, json=payload)
        except httpx.HTTPError as exc:
            logger.error("gateway internal API unreachable: %s", exc)
            raise HTTPException(502, "Внутренний API шлюза недоступен") from exc
        if response.status_code >= 400:
            raise _gateway_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise HTTPException(502, "Шлюз вернул некорректный JSON") from exc
        if not isinstance(data, dict):
            raise HTTPException(502, "Шлюз вернул неожиданный ответ")
        return data

    # --- px6 account / purchase -------------------------------------------

    async def px6_account(self) -> dict:
        data = await self._get("/internal/px6/account")
        return {
            "user_id": str(data.get("user_id") or ""),
            "balance": _as_float(data.get("balance")),
            "currency": str(data.get("currency") or "RUB"),
        }

    async def px6_countries(self, version: int = 6) -> dict:
        data = await self._get(f"/internal/px6/countries?version={version}")
        raw = data.get("countries")
        values = raw if isinstance(raw, list) else []
        countries = sorted({
            str(item).strip().lower()
            for item in values
            if str(item).strip()
        })
        return {"version": _as_int(data.get("version"), version), "countries": countries}

    async def px6_price(self, version: int = 6, count: int = 1, period: int = 30) -> dict:
        data = await self._get(
            f"/internal/px6/price?version={version}&count={count}&period={period}"
        )
        return {
            "user_id": str(data.get("user_id") or ""),
            "balance": _as_float(data.get("balance")),
            "currency": str(data.get("currency") or "RUB"),
            "price": _as_float(data.get("price")),
            "price_single": _as_float(data.get("price_single")),
            "period": _as_int(data.get("period"), period),
            "count": _as_int(data.get("count"), count),
            "version": _as_int(data.get("version"), version),
        }

    async def buy_proxies(self, payload: dict) -> dict:
        data = await self._post("/internal/px6/buy", payload)
        count = _as_int(data.get("count"), _as_int(payload.get("count"), 0))
        refresh_ok = bool(data.get("refresh_ok", True))
        if refresh_ok:
            message = f"Куплено {count} прокси. Включите их в списке."
        else:
            message = (
                f"Куплено {count} прокси, но список не обновился. "
                "Нажмите «Обновить список»."
            )
        return {
            "ok": True,
            "message": message,
            "order_id": str(data.get("order_id") or ""),
            "count": count,
            "price": _as_float(data.get("price")),
            "price_single": _as_float(data.get("price_single")),
            "period": _as_int(data.get("period"), _as_int(payload.get("period"), 0)),
            "country": str(data.get("country") or payload.get("country") or ""),
            "version": _as_int(data.get("version"), _as_int(payload.get("version"), 6)),
            "currency": str(data.get("currency") or "RUB"),
            "balance": _as_float(data.get("balance")),
            "refresh_ok": refresh_ok,
        }

    def proxy_token(self, url: str) -> str:
        secret = self.settings.internal_api_token.encode("utf-8")
        return hmac.new(secret, url.encode("utf-8"), hashlib.sha256).hexdigest()

    def _remember_proxies(self, rows: list[dict]) -> None:
        self._proxy_urls.clear()
        for row in rows:
            real = str(row.get("real_proxy") or row.get("real_url") or "")
            if real:
                self._proxy_urls[self.proxy_token(real)] = real

    async def _resolve_proxy_url(self, proxy_id: str) -> str | None:
        if proxy_id in self._proxy_urls:
            return self._proxy_urls[proxy_id]
        # The browser may have loaded a snapshot before this process started.
        await self.proxies_snapshot("all")
        return self._proxy_urls.get(proxy_id)

    # --- snapshots ---------------------------------------------------------

    async def proxies_snapshot(self, period: str = "all") -> dict:
        data = await self._get(f"/internal/proxies-data?period={quote(period)}")
        status = data.get("status")
        if not isinstance(status, dict):
            status = {}
        usage_by_proxy = data.get("usage_by_proxy")
        if not isinstance(usage_by_proxy, dict):
            usage_by_proxy = {}
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        total = usage.get("total")
        if not isinstance(total, dict):
            total = {}
        lifetime_by_proxy = data.get("usage_by_proxy_lifetime")
        if not isinstance(lifetime_by_proxy, dict):
            lifetime_by_proxy = {}

        raw_rows = status.get("proxies")
        if not isinstance(raw_rows, list):
            raw_rows = []
        self._remember_proxies([row for row in raw_rows if isinstance(row, dict)])

        rows: list[dict] = []
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            display = _masked_display(raw.get("proxy") or raw.get("url"))
            per_proxy = usage_by_proxy.get(display) or {}
            real = str(raw.get("real_proxy") or raw.get("real_url") or "")
            row = {
                "id": self.proxy_token(real) if real else None,
                "display": display,
                **_proxy_state_fields(raw),
                "px6": _px6_meta(raw.get("px6")),
                "usage": {
                    "requests": _as_int(per_proxy.get("requests", raw.get("requests", 0))),
                    "errors": _as_int(per_proxy.get("errors", raw.get("errors", 0))),
                    "tokens": _as_int(per_proxy.get("tokens", 0)),
                },
            }
            rows.append(row)

        return {
            "server_time": _now_iso(),
            "summary": {
                "requests": _as_int(total.get("requests")),
                "errors": _as_int(total.get("errors")),
                "tokens": _as_int(total.get("tokens")),
                "proxies_ready": _as_int(status.get("proxies_ready")),
                "proxies_enabled": _as_int(
                    status.get("proxies_enabled"),
                    sum(1 for row in rows if row["enabled"]),
                ),
                "proxies_total": _as_int(status.get("proxies_total"), len(rows)),
            },
            "totals": _lifetime_totals(lifetime_by_proxy),
            "proxies": rows,
            "recent": _sanitize_recent(data.get("recent")),
        }

    async def stats_snapshot(
        self, days: int = 30, period: str | None = None
    ) -> dict:
        """Token statistics for a rolling or calendar window.

        With ``period`` the window follows the Proxies page (day/week/month/all);
        otherwise the last ``days`` days are used. The gateway sends an epoch
        ``server_time`` and parallel per-day arrays; the browser gets an
        ISO-8601 UTC clock label, masked proxy names and normalized counters.
        Unknown gateway keys never pass through.
        """
        query = f"period={period}" if period else f"days={days}"
        data = await self._get(f"/internal/stats-data?{query}")

        raw_dates = data.get("dates")
        dates: list[str] = []
        if isinstance(raw_dates, list):
            dates = [item for item in raw_dates if isinstance(item, str)]
        # Series are aligned to the dates the gateway actually reported.
        length = len(dates)

        raw_totals = data.get("totals")
        if not isinstance(raw_totals, dict):
            raw_totals = {}

        raw_proxies = data.get("proxies")
        if not isinstance(raw_proxies, list):
            raw_proxies = []
        proxies: list[dict] = []
        for raw in raw_proxies:
            if not isinstance(raw, dict):
                continue
            proxies.append({
                "proxy": _masked_display(raw.get("proxy") or raw.get("url")),
                "in_pool": bool(raw.get("in_pool", False)),
                "tokens_total": _as_int(raw.get("tokens_total")),
                "requests_total": _as_int(raw.get("requests_total")),
                "errors_total": _as_int(raw.get("errors_total")),
            })
        proxies.sort(key=lambda item: (-item["tokens_total"], item["proxy"]))

        series_by_proxy: dict[str, dict] = {}
        raw_by_proxy = data.get("series_by_proxy")
        if isinstance(raw_by_proxy, dict):
            for raw_key, raw_series in raw_by_proxy.items():
                if not isinstance(raw_series, dict):
                    continue
                display = _masked_display(raw_key)
                if not display:
                    continue
                series_by_proxy[display] = _series_block(raw_series, length)

        return {
            "server_time": _iso_from_epoch(data.get("server_time")),
            "days": _as_int(data.get("days"), days),
            "dates": dates,
            "totals": {
                "tokens_total": _as_int(raw_totals.get("tokens_total")),
                "requests_total": _as_int(raw_totals.get("requests_total")),
                "errors_total": _as_int(raw_totals.get("errors_total")),
            },
            "proxies": proxies,
            "series_total": _series_block(data.get("series_total"), length),
            "series_by_proxy": series_by_proxy,
        }

    async def keys_snapshot(self, period: str = "all") -> dict:
        data = await self._get(f"/internal/keys-data?period={quote(period)}")
        raw_keys = data.get("keys")
        if not isinstance(raw_keys, list):
            raw_keys = []
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}

        keys: list[dict] = []
        by_id: dict[str, dict] = {}
        for raw in raw_keys:
            if not isinstance(raw, dict):
                continue
            key_id = str(raw.get("key_id", ""))
            raw_models = raw.get("models")
            if isinstance(raw_models, str):
                models: list[str] | None = [
                    part.strip() for part in raw_models.split(",") if part.strip()
                ] or None
            elif isinstance(raw_models, list):
                models = [str(part).strip() for part in raw_models if str(part).strip()] or None
            else:
                models = None
            row = {
                "key_hash": str(raw.get("key_hash", "")),
                "key_id": key_id,
                "name": str(raw.get("name", "")),
                "rpm": _as_optional_int(raw.get("rpm")),
                "tpm": _as_optional_int(raw.get("tpm")),
                "models": models,
                "enabled": bool(raw.get("enabled", True)),
            }
            keys.append(row)
            by_id[key_id] = row

        stats: list[dict] = []
        for key_id, raw in usage.items():
            if key_id == "total" or not isinstance(raw, dict):
                continue
            live = by_id.get(key_id)
            if live is None:
                state = "deleted"
            elif live["enabled"]:
                state = "active"
            else:
                state = "revoked"
            stats.append({
                "key_id": key_id,
                "state": state,
                "requests": _as_int(raw.get("requests")),
                "errors": _as_int(raw.get("errors")),
                "tokens": _as_int(raw.get("tokens")),
            })
        stats.sort(key=lambda item: (-item["requests"], item["key_id"]))

        return {"server_time": _now_iso(), "keys": keys, "stats": stats}

    # --- mutations ---------------------------------------------------------

    async def refresh_proxies(self) -> dict:
        data = await self._post("/internal/proxies/refresh", {})
        message = _localize(str(data.get("message") or "Proxies refreshed"))
        return {"ok": True, "message": message or "Список прокси обновлён"}

    async def set_proxy(self, proxy_id: str, enabled: bool) -> dict:
        url = await self._resolve_proxy_url(proxy_id)
        if url is None:
            raise HTTPException(404, "Прокси не найден")
        data = await self._post("/internal/proxies/set", {"proxy": url, "enabled": enabled})
        message = _localize(str(data.get("message") or ""))
        return {
            "ok": True,
            "message": message or ("Прокси включён" if enabled else "Прокси отключён"),
        }

    @staticmethod
    def _key_payload(body: KeyAddBody) -> dict:
        models = body.models
        if isinstance(models, str):
            models = [part.strip() for part in models.split(",") if part.strip()] or None
        elif isinstance(models, list):
            models = [str(part).strip() for part in models if str(part).strip()] or None
        else:
            models = None
        return {
            "key": body.key,
            "key_id": body.key_id.strip(),
            "name": body.name.strip(),
            "rpm": body.rpm,
            "tpm": body.tpm,
            "models": models,
        }

    async def add_key(self, body: KeyAddBody) -> dict:
        data = await self._post("/internal/keys/add", self._key_payload(body))
        if not data.get("ok", True):
            raise HTTPException(400, _localize(str(data.get("message") or "Не удалось добавить ключ")))
        return {"ok": True, "message": _localize(str(data.get("message") or "Ключ добавлен"))}

    async def set_key(self, key_hash: str, enabled: bool) -> dict:
        data = await self._post("/internal/keys/set", {"key_hash": key_hash, "enabled": enabled})
        message = _localize(str(data.get("message") or ""))
        return {"ok": True, "message": message or ("Ключ включён" if enabled else "Ключ отозван")}

    async def delete_key(self, key_hash: str) -> dict:
        data = await self._post("/internal/keys/delete", {"key_hash": key_hash})
        message = _localize(str(data.get("message") or ""))
        return {"ok": True, "message": message or "Ключ удалён"}

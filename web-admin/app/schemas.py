"""Request models for the OpenRoute admin JSON API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    password: str = ""


class ProxySetBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    proxy_id: str
    enabled: bool

    @field_validator("proxy_id")
    @classmethod
    def _pid_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Не указан прокси")
        return value


class ProxyBuyBody(BaseModel):
    """Purchase request (proxies are bought disabled; enable manually)."""

    model_config = ConfigDict(extra="ignore")

    count: int = 1
    period: int = 30
    country: str
    version: int = 6
    descr: str = ""

    @field_validator("count", "period", "version", mode="before")
    @classmethod
    def _int_field(cls, value: object) -> int:
        if isinstance(value, bool):
            raise ValueError("Некорректное числовое значение")
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError("Некорректное числовое значение")

    @field_validator("count")
    @classmethod
    def _count_range(cls, value: int) -> int:
        if not 1 <= value <= 1000:
            raise ValueError("Количество должно быть от 1 до 1000")
        return value

    @field_validator("period")
    @classmethod
    def _period_valid(cls, value: int) -> int:
        if value not in (3, 7, 14, 30, 60, 90):
            raise ValueError("Период должен быть 3, 7, 14, 30, 60 или 90 дней")
        return value

    @field_validator("version")
    @classmethod
    def _version_valid(cls, value: int) -> int:
        if value not in (3, 4, 6):
            raise ValueError("Версия должна быть 3 (IPv4 Shared), 4 (IPv4) или 6 (IPv6)")
        return value

    @field_validator("country")
    @classmethod
    def _country_valid(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) != 2 or not value.isascii() or not value.isalpha():
            raise ValueError("Страна должна быть кодом ISO2 (две буквы)")
        return value

    @field_validator("descr")
    @classmethod
    def _descr_valid(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 50:
            raise ValueError("Комментарий не длиннее 50 символов")
        return value


class KeyAddBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    key_id: str = ""
    name: str = ""
    rpm: int | None = None
    tpm: int | None = None
    # The UI sends a list; a comma-separated string is accepted for curl/tests.
    models: list[str] | str | None = None

    @field_validator("key")
    @classmethod
    def _key_valid(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Ключ не указан")
        if len(value) > 200:
            raise ValueError("Ключ слишком длинный (макс. 200 символов)")
        return value

    @field_validator("rpm", "tpm", mode="before")
    @classmethod
    def _limit_valid(cls, value: object, info) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError(f"Некорректный {info.field_name.upper()}: {value}")
        try:
            number = int(str(value))
        except (TypeError, ValueError):
            raise ValueError(f"Некорректный {info.field_name.upper()}: {value}")
        if number < 0:
            raise ValueError(f"{info.field_name.upper()} не может быть отрицательным")
        return number


class KeySetBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key_hash: str
    enabled: bool

    @field_validator("key_hash")
    @classmethod
    def _hash_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Не указан ключ")
        return value


class KeyDeleteBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key_hash: str

    @field_validator("key_hash")
    @classmethod
    def _hash_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Не указан ключ")
        return value

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


class KeyAddBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    key_id: str = ""
    name: str = ""
    admin: bool = False
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

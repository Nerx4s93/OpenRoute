from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ProxyItem(BaseModel):
    id: str
    version: str
    ip: str
    host: str
    port: str
    user: Optional[str] = None
    password: Optional[str] = None
    type: str
    country: str
    date: str
    date_end: str
    unixtime: int
    unixtime_end: int
    descr: str
    active: str


class ProxyListResponse(BaseModel):
    count: int
    proxies: list[ProxyItem]


class StatusResponse(BaseModel):
    status: str
    cached: bool


class AccountResponse(BaseModel):
    user_id: str
    balance: float
    currency: str


class CountriesResponse(BaseModel):
    version: int
    countries: list[str]


class PriceResponse(BaseModel):
    user_id: str = ""
    balance: float = 0.0
    currency: str = "RUB"
    price: float
    price_single: float = 0.0
    period: int
    count: int
    version: int


class BuyRequest(BaseModel):
    count: int = Field(ge=1, le=1000)
    period: int = Field(ge=1, le=365)
    country: str
    version: int = 6
    descr: str = ""
    auto_prolong: bool = False

    @field_validator("country")
    @classmethod
    def _country_valid(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) != 2 or not value.isascii() or not value.isalpha():
            raise ValueError("Страна должна быть кодом ISO2 (две буквы)")
        return value

    @field_validator("version")
    @classmethod
    def _version_valid(cls, value: int) -> int:
        if isinstance(value, bool) or value not in (3, 4, 6):
            raise ValueError("Поддерживаются версии 3 (IPv4 Shared), 4 (IPv4) и 6 (IPv6)")
        return value

    @field_validator("descr")
    @classmethod
    def _descr_valid(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 50:
            raise ValueError("Комментарий не длиннее 50 символов")
        return value


class BuyResponse(BaseModel):
    order_id: str
    count: int
    price: float
    price_single: float = 0.0
    period: int
    country: str
    version: int
    currency: str
    balance: float

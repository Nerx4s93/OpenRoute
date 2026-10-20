from typing import Optional

from pydantic import BaseModel


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

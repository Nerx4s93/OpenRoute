"""Pydantic request/response models for the storage-service API.

Field names deliberately match the columns and the dicts returned by the
gateway stores (``key_hash``, ``key_id``, ``models``, ``quota_until``, ...) so
the gateway phase can swap its persistence layer without reshaping data.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

#: SHA-256 hex digest; the only accepted key identifier (plaintext is never
#: accepted by this service).
KeyHash = Annotated[
    str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]


# --- health -----------------------------------------------------------------


class HealthResponse(BaseModel):
    status: str
    database: str


# --- proxies ----------------------------------------------------------------


class ProxyRow(BaseModel):
    proxy: str
    enabled: bool
    added_at: float
    quota_until: float
    banned_until: float
    permanently_banned: bool
    listed: bool


class ProxyListResponse(BaseModel):
    proxies: list[ProxyRow]


class ProxySyncRequest(BaseModel):
    """One successful px6 catalogue snapshot (deduped server-side).

    ``enable_new`` is the initial enabled flag for rows inserted by this
    snapshot; existing rows keep their current flag.
    """

    proxies: list[str]
    enable_new: bool = False


class ProxySyncResponse(BaseModel):
    added: int
    proxies: list[ProxyRow]


class ProxySetEnabledRequest(BaseModel):
    proxy: str = Field(min_length=1)
    enabled: bool


class ProxySetEnabledResponse(BaseModel):
    proxy: str
    enabled: bool
    updated: bool


class ProxyParkingRequest(BaseModel):
    """Parking state for one quota bucket."""

    proxies: list[str]
    quota_until: float = 0.0
    banned_until: float = 0.0
    permanently_banned: bool = False


class ProxyParkingResponse(BaseModel):
    updated: int


# --- keys -------------------------------------------------------------------


class KeyRow(BaseModel):
    key_hash: str
    key_id: str
    rpm: int | None = None
    tpm: int | None = None
    models: list[str] | None = None
    enabled: bool


class KeyListResponse(BaseModel):
    keys: list[KeyRow]


class KeyAddRequest(BaseModel):
    key_hash: KeyHash
    key_id: str = ""
    rpm: int | None = Field(default=None, ge=0)
    tpm: int | None = Field(default=None, ge=0)
    models: list[str] | None = None


class KeyAddResponse(BaseModel):
    created: bool


class KeyRotateRequest(BaseModel):
    key_hash: KeyHash
    new_key_hash: KeyHash


class KeyRotateResponse(BaseModel):
    rotated: bool
    already_rotated: bool
    key_id: str


class KeySetEnabledRequest(BaseModel):
    key_hash: KeyHash
    enabled: bool


class KeySetEnabledResponse(BaseModel):
    key_hash: str
    enabled: bool
    updated: bool


class KeyDeleteResponse(BaseModel):
    key_hash: str
    deleted: bool


# --- affinity ---------------------------------------------------------------


class AffinityBinding(BaseModel):
    session_key: str
    proxy: str | None = None
    created_at: float
    last_used_at: float


class AffinityListResponse(BaseModel):
    bindings: list[AffinityBinding]


class AffinityUpsertRequest(BaseModel):
    bindings: list[AffinityBinding]


class AffinityUpsertResponse(BaseModel):
    #: Rows written (including rows stored with ``proxy = NULL``).
    written: int
    #: Rows whose proxy no longer exists and were stored unbound.
    unbound: int


class AffinityClearRequest(BaseModel):
    session_keys: list[str]


class AffinityClearResponse(BaseModel):
    cleared: int


# --- personas ---------------------------------------------------------------


class PersonaRow(BaseModel):
    quota_key: str
    session_id: str
    project_id: str
    trace_id: str
    created_at: float
    generation: int


class PersonaListResponse(BaseModel):
    personas: list[PersonaRow]


class PersonaUpsertRequest(BaseModel):
    quota_key: str = Field(min_length=1)
    session_id: str
    project_id: str
    trace_id: str
    created_at: float
    generation: int


class PersonaUpsertResponse(BaseModel):
    #: False when the generation guard rejected a stale write.
    written: bool


# --- request log / stats ----------------------------------------------------


class LogAppendRequest(BaseModel):
    ts: float
    key_id: str
    method: str
    path: str
    model: str | None = None
    status: int
    latency_ms: float
    stream: bool = False
    account: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


class LogAppendResponse(BaseModel):
    logged: bool


class LogEntryRow(BaseModel):
    ts: float
    key_id: str
    method: str
    path: str
    model: str | None = None
    status: int
    latency_ms: float
    stream: bool
    account: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


class RecentLogResponse(BaseModel):
    entries: list[LogEntryRow]
    limit: int


class KeyUsage(BaseModel):
    requests: int
    errors: int
    tokens: int
    avg_latency_ms: float


class AccountUsage(BaseModel):
    requests: int
    errors: int
    tokens: int


class DailyUsage(BaseModel):
    day: str  # YYYY-MM-DD (UTC)
    account: str
    requests: int
    errors: int
    tokens: int


__all__ = [
    "AccountUsage",
    "AffinityBinding",
    "AffinityClearRequest",
    "AffinityClearResponse",
    "AffinityListResponse",
    "AffinityUpsertRequest",
    "AffinityUpsertResponse",
    "DailyUsage",
    "HealthResponse",
    "KeyAddRequest",
    "KeyAddResponse",
    "KeyDeleteResponse",
    "KeyHash",
    "KeyListResponse",
    "KeyRotateRequest",
    "KeyRotateResponse",
    "KeyRow",
    "KeySetEnabledRequest",
    "KeySetEnabledResponse",
    "KeyUsage",
    "LogAppendRequest",
    "LogAppendResponse",
    "LogEntryRow",
    "PersonaListResponse",
    "PersonaRow",
    "PersonaUpsertRequest",
    "PersonaUpsertResponse",
    "ProxyListResponse",
    "ProxyParkingRequest",
    "ProxyParkingResponse",
    "ProxyRow",
    "ProxySetEnabledRequest",
    "ProxySetEnabledResponse",
    "ProxySyncRequest",
    "ProxySyncResponse",
    "RecentLogResponse",
]

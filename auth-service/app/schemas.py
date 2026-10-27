"""Request/response models for the private auth-service API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    password: str = ""
    # Forwarded by web-admin from edge's trusted X-Real-IP header; used only
    # for the in-memory brute-force limiter.
    client_ip: str = "unknown"


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sid: str = ""
    csrf: str = ""
    require_csrf: bool = False


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sid: str = ""
    csrf: str = ""


__all__ = ["LoginRequest", "LogoutRequest", "ValidateRequest"]

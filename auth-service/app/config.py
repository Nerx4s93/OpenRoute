"""auth-service configuration (pydantic-settings).

The service owns the admin password, session TTL and the Redis connection.
No other component receives these values: web-admin only calls the business
API over the private ``openroute-auth-net`` network.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All auth-service env vars (see ``.env.example``)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Admin password; empty = the whole admin panel is disabled (404).
    admin_password: str = ""
    # Server-side session TTL, seconds (12 hours by default).
    admin_session_ttl_seconds: int = 43200
    # Redis session store; only auth-service is connected to this network.
    redis_url: str = "redis://redis:6379/0"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached)."""
    return Settings()


__all__ = ["Settings", "get_settings"]

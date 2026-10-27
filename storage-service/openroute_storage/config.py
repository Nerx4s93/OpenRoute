"""storage-service configuration (pydantic-settings).

Field names map to upper-case env vars case-insensitively
(``postgres_dsn`` <- ``POSTGRES_DSN``). ``postgres_dsn`` is the only place in
this service that knows the database DSN. The service has no authentication:
it is reachable only on the private ``openroute-storage-api-net`` network.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All storage-service env vars (see ``.env.example``)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    postgres_dsn: str = "postgresql://openroute:openroute@localhost:5432/openroute"

    # AsyncConnectionPool sizing/lifecycle. Startup explicitly waits for the
    # first connection (and applies the schema), so an unreachable PostgreSQL
    # fails the service start instead of degrading silently.
    db_pool_min_size: int = 1
    db_pool_max_size: int = 8
    db_pool_timeout_seconds: float = 5.0
    db_connect_timeout_seconds: int = 5
    db_startup_timeout_seconds: float = 15.0
    db_application_name: str = "openroute-storage"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached)."""
    return Settings()


__all__ = ["Settings", "get_settings"]

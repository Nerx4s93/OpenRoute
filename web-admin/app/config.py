"""web-admin configuration (pydantic-settings). Mirrors NVRoute."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Login password for the admin UI (only field on /login); empty = disabled (404).
    admin_password: str = ""
    # Admin session TTL, seconds (12 hours).
    admin_session_ttl_seconds: int = 43200
    # Redis — session store (openroute-redis-net; survives web-admin restarts).
    redis_url: str = "redis://redis:6379/0"
    # Gateway internal API base URL (openroute-net).
    gateway_internal_url: str = "http://gateway:8000"
    # Must equal INTERNAL_API_TOKEN in the gateway's .env (X-Internal-Token).
    internal_api_token: str = ""
    # Timeout for internal API requests to the gateway, seconds.
    internal_timeout_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()

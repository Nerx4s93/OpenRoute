"""web-admin configuration (pydantic-settings).

Authentication state, the admin password and Redis live in ``auth-service``.
This service only needs the auth-service URL, the gateway internal API and the
shared ``INTERNAL_API_TOKEN`` for ``/internal/*``.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Private auth-service base URL (openroute-auth-net).
    auth_service_url: str = "http://openroute-auth:8000"
    # Timeout for auth-service business calls, seconds.
    auth_timeout_seconds: float = 5.0
    # Gateway internal API base URL (openroute-net).
    gateway_internal_url: str = "http://gateway:8000"
    # Must equal INTERNAL_API_TOKEN in the gateway's .env (X-Internal-Token).
    internal_api_token: str = ""
    # Timeout for internal API requests to the gateway, seconds.
    internal_timeout_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Gateway configuration (pydantic-settings).

``Settings`` field names map to upper-case env vars case-insensitively
(``storage_service_url`` <- ``STORAGE_SERVICE_URL``). :func:`get_settings`
returns the process-wide singleton (``lru_cache``). The field set is a frozen
contract — do not rename fields: the env-var names are part of the contract.

The gateway owns no database connection: every persistent domain (proxies,
client keys, affinity, personas, request log) goes through ``storage-service``,
which is the only component that knows the PostgreSQL DSN.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

PROVIDER_NAME = "OpenRoute"


class Settings(BaseSettings):
    """All gateway env vars (see ``.env.example``).

    Field names map to upper-case env vars case-insensitively
    (``storage_service_url`` <- ``STORAGE_SERVICE_URL``). There is deliberately
    no DSN field: PostgreSQL is reached only by ``storage-service``.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- internal API (web-admin <-> gateway) ---
    internal_api_token: str = ""  # empty = internal API disabled (404)

    # --- storage-service (owns PostgreSQL; gateway has no DSN of its own) ---
    # The gateway reaches it by container name on the private
    # openroute-storage-api-net network; no token is exchanged.
    storage_service_url: str = "http://openroute-storage:8000"
    storage_timeout_seconds: float = 10.0

    # --- proxy pool source ---
    proxies_url: str = "http://openroute-px6:8000/proxies"
    # px6-service base URL for account/price/buy operations. The gateway is the
    # only component web-admin talks to; px6-service is the only one that knows
    # the px6.net API key. Docker network: openroute-px6-net.
    px6_service_url: str = "http://openroute-px6:8000"
    proxy_refresh_seconds: float = 300.0
    proxy_retry_seconds: float = 10.0

    # --- opencode upstream ---
    opencode_base_url: str = "https://opencode.ai/inference/openai/v1"
    opencode_models_url: str = "https://opencode.ai/inference/v1/models"
    opencode_model: str = "deepseek-v4.1-flash-free"
    # Local proactive per-proxy RPM cap (0/negative = disabled). Free tier is
    # daily/IP-keyed, so this is an optional safety valve, not the primary
    # limiter.
    openroute_rpm_limit: int = 0
    upstream_timeout: float = 300.0
    connect_timeout: float = 10.0

    # --- pool / breaker / 429 parking ---
    account_fail_threshold: int = 3
    account_fail_window_seconds: float = 60.0
    account_cooldown_seconds: float = 60.0
    account_cooldown_max_seconds: float = 600.0
    account_throttle_threshold: int = 3
    account_throttle_cooldown_seconds: float = 3600.0
    health_check_timeout: float = 8.0
    retry_budget_seconds: float = 90.0
    retry_backoff_base_seconds: float = 0.5
    max_body_bytes: int = 20 * 1024 * 1024
    alert_webhook_url: str = ""

    # --- sticky sessions / egress-group balancing (app/affinity.py) ---
    affinity_enabled: bool = True
    affinity_window_seconds: float = 3600.0
    affinity_weight_sessions: float = 1.0
    affinity_weight_requests: float = 1.0
    affinity_weight_tokens: float = 1.0
    quota_reset_margin_seconds: float = 60.0

    # --- synthetic client identity per proxy quota bucket ---
    # One persona (session/project/trace) per bucket, substituted into the
    # upstream headers after proxy selection; false = forward the client's own
    # identifiers (legacy behaviour).
    persona_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached)."""
    return Settings()


__all__ = ["PROVIDER_NAME", "Settings", "get_settings"]

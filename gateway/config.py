"""Gateway configuration (pydantic-settings).

``Settings`` field names map to upper-case env vars case-insensitively
(``postgres_dsn`` <- ``POSTGRES_DSN``). :func:`get_settings` returns the
process-wide singleton (``lru_cache``). The field set is a frozen contract —
do not rename fields: the env-var names are part of the contract.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

PROVIDER_NAME = "OpenRoute"


class Settings(BaseSettings):
    """All gateway env vars (see ``.env.example``).

    Field names map to upper-case env vars case-insensitively
    (``postgres_dsn`` <- ``POSTGRES_DSN``).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- internal API (web-admin <-> gateway) ---
    internal_api_token: str = ""  # empty = internal API disabled (404)
    postgres_dsn: str = "postgresql://openroute:openroute@localhost:5432/openroute"

    # --- proxy pool source ---
    proxies_url: str = "http://openroute-px6:8000/proxies"
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
    proxy_cooldown_default_seconds: float = 60.0  # when no usable Retry-After
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
    # Deprecated: 403 abusive traffic is a permanent ban now. Kept so existing
    # deployments can keep the env var (the field set is a frozen contract).
    abusive_ban_seconds: float = 86_400.0


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton (cached)."""
    return Settings()


__all__ = ["PROVIDER_NAME", "Settings", "get_settings"]

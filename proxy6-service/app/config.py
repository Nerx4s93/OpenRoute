from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    px6_api_key: str
    px6_api_base: str = "https://px6.link/api"
    cache_hours: float = 8.0


@lru_cache
def get_settings() -> Settings:
    return Settings()

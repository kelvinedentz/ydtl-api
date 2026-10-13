from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str | None = None
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    file_ttl_seconds: int = 600
    signed_url_ttl_seconds: int = 604800
    sweep_interval_seconds: int = 120
    max_file_size_mb: int = 500
    job_record_ttl_seconds: int = 3600
    rate_limit_per_minute: int = 5

    job_timeout_seconds: int = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()

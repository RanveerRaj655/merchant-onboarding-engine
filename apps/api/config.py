from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://amop:amop@localhost:5432/amop"
    database_url_sync: str = "postgresql+psycopg2://amop:amop@localhost:5432/amop"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    mock_provider_url: str = "http://localhost:8001"
    api_base_url: str = "http://localhost:8000"
    doc_storage_path: str = "./.data/docs"
    webhook_secret: str = "demo-webhook-secret"
    operator_token: str = "demo-operator-token"
    app_pepper: str = "demo-pepper-change-me"
    verification_timeout_ms: int = 30000
    extraction_confidence_min: float = 0.7
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()

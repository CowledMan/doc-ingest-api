"""Application settings for the document ingestion API."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from the environment and ``.env``."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./doc_ingest.db"
    redis_url: str = "redis://localhost:6379/0"
    queue_backend: Literal["background", "celery"] = "background"
    parser_timeout_seconds: float = 120.0
    confidence_threshold: float = 0.6
    monthly_page_quota: int = 500
    webhook_url: str | None = None
    webhook_timeout_seconds: float = 10.0

    aliyun_ocr_key: str = ""
    aliyun_ocr_endpoint: str = ""
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""

    parser_anydoc_python: Path | None = None
    parser_markitdown_python: Path | None = None
    parser_docling_python: Path | None = None

    # Rate limiting (M2 batch 1). Redis-backed limiter is a planned M3 item.
    rate_limit_ip_per_hour: int = 10
    rate_limit_key_per_minute: int = 60

    # Pricing plans rendered on the landing page; JSON list of plan objects.
    pricing_plans_json: str = (
        '[{"name":"Free","monthly_pages":100,"price_usd":0},'
        '{"name":"Starter","monthly_pages":500,"price_usd":19},'
        '{"name":"Pro","monthly_pages":5000,"price_usd":99}]'
    )

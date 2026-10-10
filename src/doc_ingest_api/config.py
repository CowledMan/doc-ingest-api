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

    parser_anydoc_python: Path | None = None
    parser_markitdown_python: Path | None = None
    parser_docling_python: Path | None = None

"""Task queue abstraction and asynchronous parse job processing."""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .exceptions import (
    OcrNotConfiguredError,
    ParserError,
    ParseResult,
    ParserTimeoutError,
)
from .models import ParseJob
from .parsers import (
    ParserRouter,
    create_parser_router,
)
from .quotas import record_page_usage


class TaskQueue(Protocol):
    """Minimal async job dispatch contract."""

    def enqueue_parse_job(self, job_id: int) -> None:
        """Queue one parse job."""
        ...


@dataclass(frozen=True)
class BackgroundTaskQueue:
    """FastAPI in-process queue used by tests and lightweight deployments."""

    background_tasks: BackgroundTasks
    session_factory: sessionmaker[Session]
    parser_router: ParserRouter
    settings: Settings

    def enqueue_parse_job(self, job_id: int) -> None:
        """Schedule the parse job to run after the HTTP response is sent."""
        self.background_tasks.add_task(
            process_parse_job,
            job_id,
            self.session_factory,
            self.parser_router,
            self.settings,
        )


@dataclass(frozen=True)
class CeleryTaskQueue:
    """Celery/Redis dispatch for production deployments."""

    settings: Settings

    def enqueue_parse_job(self, job_id: int) -> None:
        """Send the parse job to a Celery worker."""
        from celery import Celery

        app = Celery("doc_ingest_api", broker=self.settings.redis_url, backend=self.settings.redis_url)
        app.send_task("doc_ingest_api.tasks.process_parse_job", args=[job_id])


def build_task_queue(
    settings: Settings,
    *,
    background_tasks: BackgroundTasks,
    session_factory: sessionmaker[Session],
    parser_router: ParserRouter,
) -> TaskQueue:
    """Return the configured task queue implementation."""
    if settings.queue_backend == "background":
        return BackgroundTaskQueue(
            background_tasks=background_tasks,
            session_factory=session_factory,
            parser_router=parser_router,
            settings=settings,
        )
    return CeleryTaskQueue(settings=settings)


def process_parse_job(
    job_id: int,
    session_factory: sessionmaker[Session],
    parser_router: ParserRouter | None = None,
    settings: Settings | None = None,
) -> None:
    """Process a queued job, persist its result, and send the webhook."""
    runtime_settings = settings or Settings()
    router = parser_router or create_parser_router(runtime_settings)
    with session_factory.begin() as session:
        job = session.get(ParseJob, job_id)
        if job is None or job.status != "queued":
            return
        job.status = "processing"
        file_name = job.file_name
        category = job.category
        content = job.content
        webhook_url = job.webhook_url

    suffix = Path(file_name).suffix or ".bin"
    with tempfile.TemporaryDirectory() as temp_dir:
        file_path = Path(temp_dir) / f"document{suffix}"
        file_path.write_bytes(content)
        started = time.perf_counter()
        try:
            result = router.parse(file_path, category)
        except OcrNotConfiguredError as exc:
            _fail_job(session_factory, job_id, f"ocr_not_configured: {exc}")
            if webhook_url:
                _send_webhook(
                    webhook_url,
                    _webhook_payload(job_id, status="failed", error=str(exc)),
                    timeout_seconds=runtime_settings.webhook_timeout_seconds,
                )
            return
        except ParserTimeoutError as exc:
            _fail_job(session_factory, job_id, f"parser_timeout: {exc}")
            if webhook_url:
                _send_webhook(
                    webhook_url,
                    _webhook_payload(job_id, status="failed", error=str(exc)),
                    timeout_seconds=runtime_settings.webhook_timeout_seconds,
                )
            return
        except ParserError as exc:
            _fail_job(session_factory, job_id, f"parser_error: {exc}")
            if webhook_url:
                _send_webhook(
                    webhook_url,
                    _webhook_payload(job_id, status="failed", error=str(exc)),
                    timeout_seconds=runtime_settings.webhook_timeout_seconds,
                )
            return
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        metadata = _job_metadata(result, duration_ms)

    _complete_job(
        session_factory,
        job_id,
        result=result,
        metadata=metadata,
        duration_ms=duration_ms,
    )
    if webhook_url:
        _send_webhook(
            webhook_url,
            _webhook_payload(job_id, status="succeeded", result=metadata, markdown=result.markdown),
            timeout_seconds=runtime_settings.webhook_timeout_seconds,
        )


def _job_metadata(result: ParseResult, duration_ms: float) -> dict[str, Any]:
    """Build the metadata object persisted with a successful job."""
    metadata = dict(result.metadata)
    metadata.update(
        {
            "engine_used": result.engine_used,
            "duration_ms": duration_ms,
            "confidence": result.confidence,
        }
    )
    metadata.setdefault("page_count", 1)
    return metadata


def _complete_job(
    session_factory: sessionmaker[Session],
    job_id: int,
    *,
    result: ParseResult,
    metadata: Mapping[str, Any],
    duration_ms: float,
) -> None:
    """Persist a successful parse result."""
    with session_factory.begin() as session:
        job = session.get(ParseJob, job_id)
        if job is None:
            return
        job.status = "succeeded"
        job.markdown = result.markdown
        job.result_metadata = json.dumps(metadata, ensure_ascii=False)
        job.engine_used = result.engine_used
        job.confidence = result.confidence
        job.duration_ms = duration_ms
        job.pages_used = int(metadata.get("page_count", 1))
        job.error = None
        api_key_id = job.api_key_id
        pages_used = job.pages_used
    record_page_usage(session_factory, api_key_id=api_key_id, pages=pages_used)


def _fail_job(session_factory: sessionmaker[Session], job_id: int, error: str) -> None:
    """Persist a failed parse result."""
    with session_factory.begin() as session:
        job = session.get(ParseJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.error = error


def _webhook_payload(
    job_id: int,
    *,
    status: str,
    result: Mapping[str, Any] | None = None,
    markdown: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the JSON body sent to the configured webhook."""
    return {
        "job_id": job_id,
        "status": status,
        "result": dict(result) if result is not None else None,
        "markdown": markdown,
        "error": error,
    }


def _send_webhook(url: str, payload: Mapping[str, Any], *, timeout_seconds: float) -> None:
    """POST a completed job payload to the configured webhook."""
    httpx.post(url, json=dict(payload), timeout=timeout_seconds)

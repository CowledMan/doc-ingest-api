"""Tests for the Document Ingest Python SDK using mocked HTTP transports."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from doc_ingest_sdk import (
    DocIngestAPIError,
    DocIngestClient,
    JobStatus,
    ParseResult,
)
from doc_ingest_sdk.client import JobTimeoutError


def _client(handler: Any) -> DocIngestClient:
    """Create a client with an in-memory transport."""
    return DocIngestClient(
        base_url="http://gateway.test",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )


def test_parse_uses_api_key_and_returns_markdown(tmp_path: Path) -> None:
    """The client should upload multipart data and decode Markdown results."""
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["x-api-key"] = request.headers["X-API-Key"]
        assert request.url.path == "/v1/parse"
        return httpx.Response(
            200,
            json={
                "markdown": "# Parsed",
                "metadata": {"engine_used": "markitdown", "confidence": 0.9},
            },
        )

    document = tmp_path / "sample.txt"
    document.write_text("hello", encoding="utf-8")
    with _client(handler) as client:
        result = client.parse(document, category="contracts")

    assert seen_headers["x-api-key"] == "test-key"
    assert isinstance(result, ParseResult)
    assert result.markdown == "# Parsed"
    assert result.metadata["engine_used"] == "markitdown"
    assert result.metadata["confidence"] == 0.9


def test_parse_async_queues_job_and_supports_webhook(tmp_path: Path) -> None:
    """The client should post to the async endpoint and return the job id."""
    received_webhook: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/parse/async"
        content = request.content.decode("utf-8")
        received_webhook.append("http://example.test/webhook" in content)
        return httpx.Response(
            202,
            json={"job_id": 42, "status": "queued"},
        )

    document = tmp_path / "sample.txt"
    document.write_text("hello", encoding="utf-8")
    with _client(handler) as client:
        created = client.parse_async(
            document,
            webhook_url="http://example.test/webhook",
            category="contracts",
        )

    assert received_webhook == [True]
    assert created.job_id == "42"
    assert created.status == JobStatus.QUEUED


def test_get_job_decodes_succeeded_result() -> None:
    """The client should map job results to typed model objects."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/42"
        return httpx.Response(
            200,
            json={
                "job_id": 42,
                "status": "succeeded",
                "result": {
                    "markdown": "# Done",
                    "metadata": {"engine_used": "markitdown"},
                },
                "error": None,
            },
        )

    with _client(handler) as client:
        job = client.get_job(42)

    assert job.job_id == "42"
    assert job.status == JobStatus.SUCCEEDED
    assert job.result is not None
    assert job.result.markdown == "# Done"
    assert job.result.metadata["engine_used"] == "markitdown"


def test_wait_for_job_returns_completed_job(tmp_path: Path) -> None:
    """The client should poll until the job reaches a terminal state."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        status = "queued" if calls == 1 else "succeeded"
        return httpx.Response(
            200,
            json={"job_id": 42, "status": status, "result": {"markdown": "# Done"}},
        )

    document = tmp_path / "sample.txt"
    document.write_text("hello", encoding="utf-8")
    with _client(handler) as client:
        created = client.parse_async(document)
        job = client.wait_for_job(
            created.job_id,
            timeout_seconds=1.0,
            poll_interval_seconds=0.01,
        )

    assert job.status == JobStatus.SUCCEEDED
    assert calls >= 2


def test_wait_for_job_times_out() -> None:
    """The client should raise a typed timeout error instead of hanging."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/42"
        return httpx.Response(200, json={"job_id": 42, "status": "queued"})

    with _client(handler) as client, pytest.raises(JobTimeoutError):
        client.wait_for_job(42, timeout_seconds=0.05, poll_interval_seconds=0.01)


def test_http_error_is_wrapped() -> None:
    """HTTP failures should expose status and gateway detail."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"detail": {"code": "invalid_api_key", "message": "bad key"}},
        )

    with _client(handler) as client, pytest.raises(DocIngestAPIError) as exc_info:
        client.get_job(42)

    assert exc_info.value.status_code == 401
    assert "invalid_api_key" in exc_info.value.detail


def test_models_are_json_serializable() -> None:
    """SDK models should remain JSON-compatible for downstream tools."""
    from dataclasses import asdict

    from doc_ingest_sdk import Job, JobResult

    job = Job(
        job_id="42",
        status=JobStatus.SUCCEEDED,
        result=JobResult(markdown="#", metadata={"page_count": 1}),
    )
    assert asdict(job)["status"] == "succeeded"

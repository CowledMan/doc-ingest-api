"""Tests for the FastAPI gateway, authentication, quota, routing, and jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from doc_ingest_api.app import create_app
from doc_ingest_api.config import Settings
from doc_ingest_api.database import Database, create_database
from doc_ingest_api.models import ApiKey
from doc_ingest_api.parsers import (
    AliyunOcrStubParser,
    OcrNotConfiguredError,
    ParserError,
    ParseResult,
    ParserRouter,
    ParserTimeoutError,
)
from doc_ingest_api.security import create_api_key


@dataclass
class MockParser:
    """Deterministic parser used for router and gateway tests."""

    engine: str
    markdown: str = "# Mock\n\ncontent"
    confidence: float = 0.9
    metadata: dict[str, Any] = field(default_factory=dict)
    fail_with: Exception | None = None
    calls: list[Path] = field(default_factory=list)

    def parse(self, file: Path) -> ParseResult:
        """Record the call and return a deterministic result."""
        self.calls.append(file)
        if self.fail_with is not None:
            raise self.fail_with
        return ParseResult(
            markdown=self.markdown,
            confidence=self.confidence,
            metadata=dict(self.metadata),
            engine_used=self.engine,
        )


def _settings(**overrides: Any) -> Settings:
    """Create isolated API settings."""
    values: dict[str, Any] = {
        "database_url": "sqlite://",
        "queue_backend": "background",
        "confidence_threshold": 0.6,
        "monthly_page_quota": 500,
        "parser_timeout_seconds": 0.01,
    }
    values.update(overrides)
    return Settings(**values)


def _app_with_parser(
    parser: Any,
    *,
    settings: Settings | None = None,
) -> tuple[FastAPI, Database, str]:
    """Create an app backed by in-memory SQLite and one mock parser."""
    runtime_settings = settings or _settings()
    database = create_database(runtime_settings.database_url)
    database.create_all()
    router = ParserRouter(
        parsers={"markitdown": parser, "anydoc": parser},
        confidence_threshold=runtime_settings.confidence_threshold,
    )
    app = create_app(
        runtime_settings,
        session_factory=database.session_factory,
        parser_router=router,
    )
    api_key = create_api_key(database.session_factory, name="test")
    return app, database, api_key


def test_healthz_does_not_require_authentication() -> None:
    app, _, _ = _app_with_parser(MockParser("markitdown"))
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_parse_requires_api_key() -> None:
    app, _, _ = _app_with_parser(MockParser("markitdown"))
    with TestClient(app) as client:
        response = client.post(
            "/v1/parse",
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "missing_api_key"


def test_parse_rejects_unknown_api_key() -> None:
    app, _, _ = _app_with_parser(MockParser("markitdown"))
    with TestClient(app) as client:
        response = client.post(
            "/v1/parse",
            headers={"X-API-Key": "invalid"},
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_api_key"


def test_sync_parse_returns_markdown_and_metadata() -> None:
    parser = MockParser(
        "markitdown",
        markdown="# Parsed",
        confidence=0.8,
        metadata={"page_count": 2},
    )
    app, database, api_key = _app_with_parser(parser)
    with TestClient(app) as client:
        response = client.post(
            "/v1/parse",
            headers={"X-API-Key": api_key},
            files={"file": ("sample.txt", b"hello", "text/plain")},
            data={"category": "contracts"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["markdown"] == "# Parsed"
    assert payload["metadata"]["engine_used"] == "markitdown"
    assert payload["metadata"]["confidence"] == 0.8
    assert "duration_ms" in payload["metadata"]
    with database.session_factory() as session:
        key = session.get(ApiKey, 1)
        assert key is not None
        assert key.used_pages == 2


def test_sync_parse_returns_429_when_quota_exhausted() -> None:
    app, database, api_key = _app_with_parser(MockParser("markitdown"))
    with database.session_factory.begin() as session:
        key = session.get(ApiKey, 1)
        assert key is not None
        key.used_pages = key.monthly_page_quota

    with TestClient(app) as client:
        response = client.post(
            "/v1/parse",
            headers={"X-API-Key": api_key},
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "quota_exceeded"


def test_scanned_document_reports_missing_ocr_configuration() -> None:
    settings = _settings()
    database = create_database(settings.database_url)
    database.create_all()
    router = ParserRouter(
        parsers={"ocr": AliyunOcrStubParser(api_key="")},
        confidence_threshold=settings.confidence_threshold,
    )
    app = create_app(
        settings,
        session_factory=database.session_factory,
        parser_router=router,
    )
    api_key = create_api_key(database.session_factory, name="scan")
    with TestClient(app) as client:
        response = client.post(
            "/v1/parse",
            headers={"X-API-Key": api_key},
            files={"file": ("scan.pdf", b"%PDF-", "application/pdf")},
            data={"category": "scanned_documents"},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "ocr_not_configured"


def test_async_parse_runs_job_and_sends_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = MockParser("markitdown", markdown="# Async", metadata={"page_count": 1})
    app, database, api_key = _app_with_parser(parser)
    sent: list[tuple[str, dict[str, Any]]] = []

    def fake_send_webhook(url: str, payload: dict[str, Any], *, timeout_seconds: float) -> None:
        sent.append((url, payload))
        assert timeout_seconds > 0

    monkeypatch.setattr("doc_ingest_api.tasks._send_webhook", fake_send_webhook)
    with TestClient(app) as client:
        created = client.post(
            "/v1/parse/async",
            headers={"X-API-Key": api_key},
            files={"file": ("sample.txt", b"hello", "text/plain")},
            data={"category": "contracts", "webhook_url": "http://example.test/webhook"},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        status = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key})
    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == "succeeded"
    assert payload["result"]["markdown"] == "# Async"
    assert payload["result"]["metadata"]["engine_used"] == "markitdown"
    assert len(sent) == 1
    assert sent[0][0] == "http://example.test/webhook"
    assert sent[0][1]["status"] == "succeeded"


def test_async_job_is_isolated_by_api_key() -> None:
    parser = MockParser("markitdown")
    app, database, first_key = _app_with_parser(parser)
    second_key = create_api_key(database.session_factory, name="second")
    with TestClient(app) as client:
        created = client.post(
            "/v1/parse/async",
            headers={"X-API-Key": first_key},
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
        job_id = created.json()["job_id"]
        response = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": second_key})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "job_not_found"


def test_router_prefers_financial_reports_anydoc() -> None:
    markitdown = MockParser("markitdown", confidence=0.95)
    anydoc = MockParser("anydoc", confidence=0.95)
    router = ParserRouter({"markitdown": markitdown, "anydoc": anydoc})
    result = router.parse(Path("report.pdf"), "financial_reports")
    assert result.engine_used == "anydoc"
    assert markitdown.calls == []
    assert anydoc.calls


def test_router_falls_back_on_low_confidence() -> None:
    markitdown = MockParser("markitdown", confidence=0.1)
    anydoc = MockParser("anydoc", confidence=0.8)
    router = ParserRouter({"markitdown": markitdown, "anydoc": anydoc})
    result = router.parse(Path("sample.txt"))
    assert result.engine_used == "anydoc"
    assert len(markitdown.calls) == 1


def test_router_ignores_parser_errors_until_success() -> None:
    markitdown = MockParser("markitdown", fail_with=ParserError("boom"))
    anydoc = MockParser("anydoc")
    router = ParserRouter({"markitdown": markitdown, "anydoc": anydoc})
    result = router.parse(Path("sample.txt"))
    assert result.engine_used == "anydoc"


def test_router_propagates_timeout_and_ocr_errors(tmp_path: Path) -> None:
    timeout_parser = MockParser("markitdown", fail_with=ParserTimeoutError("late"))
    router = ParserRouter({"markitdown": timeout_parser})
    with pytest.raises(ParserTimeoutError):
        router.parse(tmp_path / "sample.txt")

    ocr_parser = MockParser("ocr", fail_with=OcrNotConfiguredError("no key"))
    router = ParserRouter({"ocr": ocr_parser})
    with pytest.raises(OcrNotConfiguredError):
        router.parse(tmp_path / "scan.pdf", "scanned_documents")


def test_async_parse_job_failure_is_reported() -> None:
    parser = MockParser("markitdown", fail_with=ParserError("boom"))
    app, database, api_key = _app_with_parser(parser)
    with TestClient(app) as client:
        created = client.post(
            "/v1/parse/async",
            headers={"X-API-Key": api_key},
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
        job_id = created.json()["job_id"]
        response = client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["error"].startswith("parser_error")


def test_webhook_payload_is_json_serializable() -> None:
    from doc_ingest_api.tasks import _webhook_payload

    payload = _webhook_payload(1, status="succeeded", result={"confidence": 0.9}, markdown="#")
    assert json.loads(json.dumps(payload))["status"] == "succeeded"

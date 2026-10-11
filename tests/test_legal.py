"""Tests for legal compliance pages (Paddle domain review requirement)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from doc_ingest_api.config import Settings
from doc_ingest_api.database import create_database
from tests.test_api import MockParser


def _app() -> TestClient:
    """Create a minimal test app without authentication requirements."""
    settings = Settings(
        database_url="sqlite://",
        queue_backend="background",
        parser_timeout_seconds=0.01,
        confidence_threshold=0.6,
        monthly_page_quota=500,
    )
    database = create_database(settings.database_url)
    database.create_all()
    from doc_ingest_api.app import create_app

    parser = MockParser("markitdown")
    app = create_app(
        settings,
        session_factory=database.session_factory,
        parser_router=type("R", (), {"parse": lambda self, path, category=None: parser.parse(path)})(),
    )
    return TestClient(app)


class TestLegalPages:
    """Legal pages return 200 and contain required brand name."""

    def test_terms_page_returns_200_and_doingest(self) -> None:
        client = _app()
        response = client.get("/terms")
        assert response.status_code == 200
        assert "DoIngest" in response.text
        assert "Terms" in response.text
        assert "Paddle.com" in response.text

    def test_refund_page_returns_200_and_doingest(self) -> None:
        client = _app()
        response = client.get("/refund")
        assert response.status_code == 200
        assert "DoIngest" in response.text
        assert "Refund" in response.text
        assert "Merchant of Record" in response.text

    def test_privacy_page_returns_200_and_doingest(self) -> None:
        client = _app()
        response = client.get("/privacy")
        assert response.status_code == 200
        assert "DoIngest" in response.text
        assert "Privacy" in response.text
        assert "GDPR" in response.text
        assert "CCPA" in response.text

    def test_terms_has_acceptable_use_and_liability(self) -> None:
        client = _app()
        response = client.get("/terms")
        assert "Acceptable Use" in response.text
        assert "Limitation of Liability" in response.text
        assert "Governing Law" in response.text
        assert "support@tomdocs.com" in response.text

    def test_refund_has_14_day_policy(self) -> None:
        client = _app()
        response = client.get("/refund")
        assert "fourteen (14) days" in response.text
        assert "Cancellation" in response.text
        assert "support@tomdocs.com" in response.text

    def test_privacy_has_data_retention_and_rights(self) -> None:
        client = _app()
        response = client.get("/privacy")
        assert "Data Retention" in response.text
        assert "Your Rights" in response.text
        assert "support@tomdocs.com" in response.text

    def test_landing_contains_legal_footer_links(self) -> None:
        client = _app()
        response = client.get("/")
        assert response.status_code == 200
        assert 'href="/terms"' in response.text
        assert 'href="/refund"' in response.text
        assert 'href="/privacy"' in response.text

    def test_legal_pages_have_footer_navigation(self) -> None:
        client = _app()
        for path in ("/terms", "/refund", "/privacy"):
            response = client.get(path)
            assert 'href="/terms"' in response.text, f"missing terms link on {path}"
            assert 'href="/refund"' in response.text, f"missing refund link on {path}"
            assert 'href="/privacy"' in response.text, f"missing privacy link on {path}"
            assert 'href="/"' in response.text, f"missing home link on {path}"

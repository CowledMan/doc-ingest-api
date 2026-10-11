"""Tests for branded API documentation pages."""

from __future__ import annotations

from fastapi.testclient import TestClient

from doc_ingest_api.config import Settings
from doc_ingest_api.database import create_database
from tests.test_api import MockParser


def _app() -> TestClient:
    """Create a minimal test app."""
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


class TestDocsPage:
    """GET /docs serves branded API documentation."""

    def test_docs_returns_200(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert response.status_code == 200
        assert "DoIngest API Documentation" in response.text

    def test_docs_has_quickstart(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert "Quickstart" in response.text
        assert "tomdocs.com/v1/parse" in response.text
        assert "X-API-Key" in response.text

    def test_docs_has_authentication(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert "Authentication" in response.text
        assert "dip_" in response.text

    def test_docs_has_endpoints_table(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert "Endpoints" in response.text
        assert "/v1/parse" in response.text
        assert "/v1/demo-keys" in response.text
        assert "/v1/billing/portal" in response.text
        assert "/v1/keys/recover" in response.text
        assert "/healthz" in response.text

    def test_docs_has_error_codes(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert "Error Codes" in response.text
        assert "invalid_api_key" in response.text
        assert "quota_exceeded" in response.text
        assert "rate_limited" in response.text
        assert "invalid_email" in response.text

    def test_docs_has_rate_limits(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert "Rate Limits" in response.text
        assert "50" in response.text
        assert "60" in response.text

    def test_docs_has_sdk_and_mcp(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert "SDK &amp; MCP" in response.text
        assert "doc_ingest_sdk" in response.text
        assert "mcpServers" in response.text
        assert "doc-ingest-mcp" in response.text

    def test_docs_has_toc(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert "On this page" in response.text
        assert 'href="#quickstart"' in response.text
        assert 'href="#endpoints"' in response.text

    def test_docs_has_cta(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert "Get Free API Key" in response.text
        assert "Open Interactive Docs" in response.text
        assert 'href="/docs/swagger"' in response.text

    def test_docs_has_footer(self) -> None:
        client = _app()
        response = client.get("/docs")
        assert "Shandong Tengyu Electronic Technology" in response.text
        assert "/terms" in response.text
        assert "support@tomdocs.com" in response.text


class TestSwaggerRedirect:
    """Swagger UI is accessible at /docs/swagger."""

    def test_swagger_still_accessible(self) -> None:
        client = _app()
        response = client.get("/docs/swagger")
        assert response.status_code == 200
        assert "swagger" in response.text.lower()

    def test_openapi_json_unchanged(self) -> None:
        client = _app()
        response = client.get("/openapi.json")
        assert response.status_code == 200
        assert response.json()["info"]["title"] == "Document Ingest API"

    def test_landing_links_to_branded_docs(self) -> None:
        client = _app()
        response = client.get("/")
        assert 'href="/docs"' in response.text

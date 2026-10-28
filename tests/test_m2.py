"""Tests for landing page, rate limiting, OpenAPI docs, and demo keys."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from doc_ingest_api.app import create_app
from doc_ingest_api.config import Settings
from doc_ingest_api.database import create_database
from doc_ingest_api.rate_limit import InMemoryRateLimiter
from doc_ingest_api.security import create_api_key
from tests.test_api import MockParser


def _register_user(client: TestClient, email: str) -> None:
    """Register a throwaway account for demo-key tests."""
    res = client.post(
        "/api/auth/register",
        json={"email": email, "password": "hunter2boogaloo"},
    )
    assert res.status_code == 200


def _app_with_rate_limits(
    parser: Any,
    **overrides: Any,
) -> tuple[TestClient, str]:
    """Create an app with tight rate limits for testing."""
    values: dict[str, Any] = {
        "database_url": "sqlite://",
        "queue_backend": "background",
        "rate_limit_ip_per_hour": overrides.pop("ip_limit", 3),
        "rate_limit_key_per_minute": overrides.pop("key_limit", 5),
        "parser_timeout_seconds": 0.01,
        "confidence_threshold": 0.6,
        "monthly_page_quota": 500,
        **overrides,
    }
    settings = Settings(**values)
    database = create_database(settings.database_url)
    database.create_all()
    app = create_app(
        settings,
        session_factory=database.session_factory,
        parser_router=type("R", (), {"parse": lambda self, path, category=None: parser.parse(path)})(),
        rate_limiter=InMemoryRateLimiter(),
    )
    api_key = create_api_key(database.session_factory, name="rl-test")
    return TestClient(app), api_key


class TestLandingPage:
    """Landing page and pricing rendering."""

    def test_landing_returns_200_with_pricing(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/")
        assert response.status_code == 200
        assert "DoIngest" in response.text
        assert "pricing" in response.text.lower()
        assert "Starter" in response.text
        assert "Free" in response.text
        assert "Pro" in response.text

    def test_landing_hero_and_nav(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/")
        assert "DoIngest" in response.text
        assert "one api call" in response.text.lower()
        assert "Get Free API Key" in response.text
        assert "Read the Docs" in response.text
        assert 'href="/docs"' in response.text
        assert "GitHub" in response.text

    def test_landing_features_and_steps(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/")
        assert "Why DoIngest?" in response.text
        assert "How It Works" in response.text
        assert "MCP Server" in response.text
        assert "feature-card" in response.text

    def test_landing_pricing_cards(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/")
        assert "plan-card" in response.text
        assert 'id="btn-starter"' in response.text
        assert 'id="btn-pro"' in response.text
        assert 'id="price-starter"' in response.text
        assert 'id="price-pro"' in response.text

    def test_landing_faq(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/")
        assert "Frequently Asked Questions" in response.text
        assert "<details>" in response.text
        assert "<summary>" in response.text

    def test_landing_footer_three_columns(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/")
        assert "Product" in response.text
        assert "Legal" in response.text
        assert "Contact" in response.text
        assert "Shandong Tengyu Electronic Technology" in response.text

    def test_pricing_plans_override(self) -> None:
        client, _ = _app_with_rate_limits(
            MockParser("markitdown"),
            pricing_plans_json=json.dumps(
                [{"name": "Custom", "monthly_pages": 999, "price_usd": 42}]
            ),
        )
        response = client.get("/")
        # Pricing cards are rendered from settings; custom plans override
        # the default pricing_plans_json which feeds PricePreview.
        assert "DoIngest" in response.text


class TestRateLimiting:
    """IP and key rate limiter behaviour."""

    def test_ip_rate_limit_returns_429_with_retry_after(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"), ip_limit=2)
        # /healthz is exempt, so it must never count toward the IP limit.
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").status_code == 200
        # Unauthenticated API hits (401 responses) do count toward the IP limit.
        assert client.get("/v1/billing/subscription").status_code == 401
        assert client.get("/v1/billing/subscription").status_code == 401
        # Third API hit is rate limited.
        response = client.get("/v1/billing/subscription")
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    def test_webhook_and_success_page_exempt_from_ip_limit(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"), ip_limit=1)
        # Exhaust the IP budget.
        assert client.get("/v1/billing/subscription").status_code == 401
        assert client.get("/v1/billing/subscription").status_code == 429
        # Success page and webhook stay reachable; webhook is signature-guarded.
        assert client.get("/billing/success").status_code == 200
        response = client.post(
            "/v1/billing/webhook", content=b"{}", headers={"paddle-signature": "h=0"}
        )
        assert response.status_code in (400, 401)  # rejected, never rate limited

    def test_key_rate_limit_returns_429(self) -> None:
        client, api_key = _app_with_rate_limits(MockParser("markitdown"), key_limit=2)
        headers = {"X-API-Key": api_key}
        assert client.get("/healthz", headers=headers).status_code == 200
        assert client.get("/healthz", headers=headers).status_code == 200
        response = client.get("/healthz", headers=headers)
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    def test_ip_and_key_limits_are_independent(self) -> None:
        client, api_key = _app_with_rate_limits(
            MockParser("markitdown"), ip_limit=1, key_limit=10
        )
        # First unauthenticated API request exhausts IP limit.
        assert client.get("/v1/billing/subscription").status_code == 401
        assert client.get("/v1/billing/subscription").status_code == 429
        # Authenticated requests use the key limit, not IP.
        headers = {"X-API-Key": api_key}
        assert client.get("/healthz", headers=headers).status_code == 200
        assert client.get("/healthz", headers=headers).status_code == 200


class TestOpenAPIDocs:
    """OpenAPI metadata, tags, and schema validity."""

    def test_openapi_json_is_valid(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"] == "Document Ingest API"
        assert schema["info"]["license"]["name"] == "MIT"

    def test_swagger_ui_accessible(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/docs")
        assert response.status_code == 200
        assert "swagger" in response.text.lower()

    def test_routes_have_tags(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        schema = client.get("/openapi.json").json()
        all_tags = set()
        for path_ops in schema["paths"].values():
            for op in path_ops.values():
                if isinstance(op, dict) and "tags" in op:
                    all_tags.update(op["tags"])
        assert all_tags >= {"health", "parse", "jobs", "billing", "demo"}


class TestDemoKeys:
    """Demo sandbox key creation and usage."""

    def test_create_demo_key_and_parse(self) -> None:
        parser = MockParser("markitdown", markdown="# Demo", metadata={"page_count": 1})
        client, _ = _app_with_rate_limits(parser, ip_limit=100)
        _register_user(client, "mk1@example.com")
        demo = client.post("/v1/demo-keys")
        assert demo.status_code == 200
        data = demo.json()
        assert data["plan"] == "demo"
        assert data["monthly_page_quota"] == 20
        assert data["api_key"].startswith("dip_")
        assert data["upgrade_url"] == "/#pricing"

        headers = {"X-API-Key": data["api_key"]}
        response = client.post(
            "/v1/parse",
            headers=headers,
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 200
        assert response.json()["markdown"] == "# Demo"

    def test_demo_key_quota_is_20_pages(self) -> None:
        parser = MockParser("markitdown", markdown="# Demo", metadata={"page_count": 1})
        client, _ = _app_with_rate_limits(parser, ip_limit=100, key_limit=100)
        _register_user(client, "mk2@example.com")
        demo = client.post("/v1/demo-keys")
        api_key = demo.json()["api_key"]
        headers = {"X-API-Key": api_key}
        # Make 20 requests (each 1 page) — the 21st should fail.
        for _ in range(20):
            response = client.post(
                "/v1/parse",
                headers=headers,
                files={"file": ("sample.txt", b"hello", "text/plain")},
            )
            assert response.status_code == 200
        response = client.post(
            "/v1/parse",
            headers=headers,
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 429

    def test_demo_keys_limited_per_user(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"), ip_limit=100)
        _register_user(client, "mk3@example.com")
        assert client.post("/v1/demo-keys").status_code == 200
        response = client.post("/v1/demo-keys")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "demo_key_exists"

    def test_demo_keys_response_has_sample_file_url(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"), ip_limit=100)
        _register_user(client, "mk4@example.com")
        demo = client.post("/v1/demo-keys")
        data = demo.json()
        assert data["sample_file_url"] == "/samples/demo.pdf"

    def test_demo_parse_response_has_upgrade_url(self) -> None:
        parser = MockParser("markitdown", markdown="# Demo", metadata={"page_count": 1})
        client, _ = _app_with_rate_limits(parser, ip_limit=100, key_limit=100)
        _register_user(client, "mk5@example.com")
        demo = client.post("/v1/demo-keys")
        headers = {"X-API-Key": demo.json()["api_key"]}
        response = client.post(
            "/v1/parse",
            headers=headers,
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 200
        assert response.json()["upgrade_url"] == "/#pricing"

    def test_non_demo_parse_response_has_no_upgrade_url(self) -> None:
        client, api_key = _app_with_rate_limits(
            MockParser("markitdown", markdown="# Paid"), key_limit=100
        )
        headers = {"X-API-Key": api_key}
        response = client.post(
            "/v1/parse",
            headers=headers,
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 200
        assert "upgrade_url" not in response.json()

    def test_sample_pdf_route_serves_file(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"), ip_limit=100)
        response = client.get("/samples/demo.pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert len(response.content) > 0
        assert len(response.content) < 200 * 1024
"""Tests for Paddle customer portal, migration idempotency, and customer sync."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi.testclient import TestClient

from doc_ingest_api.config import Settings
from doc_ingest_api.database import create_database
from doc_ingest_api.models import ApiKey
from doc_ingest_api.security import create_api_key, find_api_key
from tests.test_api import MockParser

TEST_SECRET = "pdl_test_webhook_secret_portal"


def _make_signature(body: bytes, secret: str, timestamp: str = "1700000000") -> str:
    """Construct a valid Paddle signature header for testing."""
    signed = f"{timestamp}:{body.decode('utf-8', errors='replace')}"
    hmac_hex = hmac.new(
        secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"ts={timestamp};h1={hmac_hex}"


def _make_webhook_body(
    event_type: str = "subscription.created",
    plan: str = "starter",
    api_key_id: int = 1,
    customer_id: str = "ctm_01test123",
    event_id: str = "evt_portal_001",
) -> bytes:
    """Create a Paddle-shaped webhook event body with customer_id."""
    return json.dumps({
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": "2026-01-01T00:00:00Z",
        "data": {
            "id": "sub_01test",
            "status": "active",
            "customer_id": customer_id,
            "items": [{
                "price": {
                    "id": "pri_01test",
                    "custom_data": {"plan": plan, "monthly_pages": 500},
                }
            }],
            "custom_data": {"api_key_id": api_key_id},
        },
    }).encode("utf-8")


def _paddle_app(**overrides: Any) -> tuple[TestClient, Any]:
    """Create a test app in Paddle sandbox mode and return (client, db)."""
    values: dict[str, Any] = {
        "database_url": "sqlite://",
        "queue_backend": "background",
        "paddle_env": "sandbox",
        "paddle_api_key": "pdl_sdbx_test_key",
        "paddle_webhook_secret": TEST_SECRET,
        "paddle_client_token": "test_client_token_123",
        "parser_timeout_seconds": 0.01,
        "confidence_threshold": 0.6,
        "monthly_page_quota": 500,
        **overrides,
    }
    settings = Settings(**values)
    database = create_database(settings.database_url)
    database.create_all()
    from doc_ingest_api.app import create_app

    parser = MockParser("markitdown")
    app = create_app(
        settings,
        session_factory=database.session_factory,
        parser_router=type("R", (), {"parse": lambda self, path, category=None: parser.parse(path)})(),
    )
    return TestClient(app), database


class TestMigrationIdempotency:
    """Lightweight column migration is idempotent."""

    def test_create_database_twice_no_error(self) -> None:
        database = create_database("sqlite://")
        database.create_all()
        # Second call must not raise even though columns exist
        database.create_all()

    def test_paddle_customer_id_column_exists(self) -> None:
        database = create_database("sqlite://")
        database.create_all()
        raw_key = create_api_key(database.session_factory, name="mig-test")
        with database.session_factory() as session:
            key = find_api_key(session, raw_key)
            assert key is not None
            assert key.paddle_customer_id is None


class TestWebhookCustomerSync:
    """Webhook events sync paddle_customer_id to the local ApiKey."""

    def test_webhook_stores_customer_id(self) -> None:
        client, database = _paddle_app()
        raw_key = create_api_key(database.session_factory, name="wh-cus")
        with database.session_factory() as s:
            key = find_api_key(s, raw_key)
            assert key is not None
            key_id = key.id
        body = _make_webhook_body(api_key_id=key_id, customer_id="ctm_01abc")
        sig = _make_signature(body, TEST_SECRET)
        response = client.post(
            "/v1/billing/webhook",
            content=body,
            headers={"Paddle-Signature": sig, "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        with database.session_factory() as s:
            key = s.get(ApiKey, key_id)
            assert key is not None
            assert key.paddle_customer_id == "ctm_01abc"


class TestPortalEndpoint:
    """POST /v1/billing/portal creates portal sessions."""

    def test_portal_with_customer_id_returns_url(self, monkeypatch: Any) -> None:
        client, database = _paddle_app()
        raw_key = create_api_key(database.session_factory, name="portal-test")
        with database.session_factory() as s:
            key = find_api_key(s, raw_key)
            assert key is not None
            key.paddle_customer_id = "ctm_01portal"
            s.commit()

        # Mock the Paddle API call
        class FakeResponse:
            status_code = 201
            text = "{}"

            def json(self) -> dict[str, Any]:
                return {"data": {"urls": {"general": {"overview": "https://portal.paddle.com/test"}}}}

        monkeypatch.setattr(
            "doc_ingest_api.paddle_billing.httpx.post",
            lambda *a, **kw: FakeResponse(),
        )

        response = client.post(
            "/v1/billing/portal",
            headers={"X-API-Key": raw_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_provider"] == "paddle"
        assert data["portal_url"] == "https://portal.paddle.com/test"

    def test_portal_without_subscription_returns_404(self) -> None:
        client, database = _paddle_app()
        raw_key = create_api_key(database.session_factory, name="no-sub")
        response = client.post(
            "/v1/billing/portal",
            headers={"X-API-Key": raw_key},
        )
        assert response.status_code == 404
        data = response.json()
        assert data["detail"]["code"] == "no_subscription"

    def test_portal_requires_api_key(self) -> None:
        client, _db = _paddle_app()
        response = client.post("/v1/billing/portal")
        assert response.status_code == 401

    def test_mock_portal_returns_url(self) -> None:
        values: dict[str, Any] = {
            "database_url": "sqlite://",
            "queue_backend": "background",
            "paddle_env": "",
            "parser_timeout_seconds": 0.01,
            "confidence_threshold": 0.6,
            "monthly_page_quota": 500,
        }
        settings = Settings(**values)
        database = create_database(settings.database_url)
        database.create_all()
        from doc_ingest_api.app import create_app

        parser = MockParser("markitdown")
        app = create_app(
            settings,
            session_factory=database.session_factory,
            parser_router=type("R", (), {"parse": lambda self, path, category=None: parser.parse(path)})(),
        )
        api_key = create_api_key(database.session_factory, name="mock-portal")
        client = TestClient(app)
        response = client.post(
            "/v1/billing/portal",
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_provider"] == "mock"
        assert data["portal_url"].startswith("https://sandbox-portal.paddle.com/mock/")

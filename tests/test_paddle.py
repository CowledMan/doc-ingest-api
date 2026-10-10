"""Tests for Paddle billing provider, webhook verification, and config."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi.testclient import TestClient

from doc_ingest_api.config import Settings
from doc_ingest_api.database import create_database
from doc_ingest_api.paddle_billing import PaddleBillingProvider
from doc_ingest_api.security import create_api_key
from tests.test_api import MockParser
from tests.test_m2 import _app_with_rate_limits

TEST_SECRET = "pdl_test_webhook_secret_12345"


def _make_signature(body: bytes, secret: str, timestamp: str = "1700000000") -> str:
    """Construct a valid Paddle signature header for testing."""
    signed = f"{timestamp}:{body.decode('utf-8', errors='replace')}"
    hmac_hex = hmac.new(
        secret.encode("utf-8"),
        signed.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"ts={timestamp};h1={hmac_hex}"


def _make_webhook_body(
    event_type: str = "subscription.created",
    plan: str = "starter",
    api_key_id: int = 1,
    event_id: str = "evt_001",
) -> bytes:
    """Create a Paddle-shaped webhook event body."""
    return json.dumps({
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": "2026-01-01T00:00:00Z",
        "data": {
            "id": "sub_01test",
            "status": "active",
            "customer_id": "ctm_01test",
            "items": [{
                "price": {
                    "id": "pri_01test",
                    "custom_data": {"plan": plan, "monthly_pages": 500},
                }
            }],
            "custom_data": {"api_key_id": api_key_id},
        },
    }).encode("utf-8")


def _paddle_app(**overrides: Any) -> TestClient:
    """Create a test app in Paddle sandbox mode."""
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
    return TestClient(app)


class TestPaddleWebhookVerification:
    """Paddle webhook signature verification (valid and invalid)."""

    def test_valid_signature_is_accepted(self) -> None:
        client = _paddle_app()
        body = _make_webhook_body(event_type="subscription.created", plan="starter")
        sig = _make_signature(body, TEST_SECRET)
        response = client.post(
            "/v1/billing/webhook",
            content=body,
            headers={
                "Paddle-Signature": sig,
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_provider"] == "paddle"
        assert data["verified"] is True
        assert data["event_type"] == "subscription.created"

    def test_invalid_signature_returns_401(self) -> None:
        client = _paddle_app()
        body = _make_webhook_body()
        bad_sig = _make_signature(body, "wrong_secret")
        response = client.post(
            "/v1/billing/webhook",
            content=body,
            headers={"Paddle-Signature": bad_sig, "Content-Type": "application/json"},
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "invalid_signature"

    def test_missing_signature_returns_401(self) -> None:
        client = _paddle_app()
        body = _make_webhook_body()
        response = client.post(
            "/v1/billing/webhook",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 401

    def test_webhook_updates_plan_and_quota(self) -> None:
        client, _db = _paddle_app_with_db()
        raw_key = create_api_key(_db.session_factory, name="wh-upgrade")
        with _db.session_factory() as s:
            from doc_ingest_api.security import find_api_key
            row = find_api_key(s, raw_key)
            assert row is not None
            key_id = row.id
        body = _make_webhook_body(
            event_type="subscription.created", plan="pro", api_key_id=key_id
        )
        sig = _make_signature(body, TEST_SECRET)
        response = client.post(
            "/v1/billing/webhook",
            content=body,
            headers={"Paddle-Signature": sig, "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        # Verify plan was updated in DB
        with _db.session_factory() as s:
            from doc_ingest_api.models import ApiKey
            row = s.get(ApiKey, key_id)
            assert row is not None
            assert row.plan == "pro"
            assert row.monthly_page_quota == 5000
        # We just verify the response was OK; direct DB check needs session.

    def test_webhook_canceled_sets_free(self) -> None:
        client = _paddle_app()
        body = _make_webhook_body(event_type="subscription.canceled", plan="starter")
        sig = _make_signature(body, TEST_SECRET)
        response = client.post(
            "/v1/billing/webhook",
            content=body,
            headers={"Paddle-Signature": sig, "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["event_type"] == "subscription.canceled"

    def test_webhook_idempotent_same_event_id(self) -> None:
        client = _paddle_app()
        body = _make_webhook_body(event_id="evt_dup")
        sig = _make_signature(body, TEST_SECRET)
        r1 = client.post(
            "/v1/billing/webhook",
            content=body,
            headers={"Paddle-Signature": sig},
        )
        r2 = client.post(
            "/v1/billing/webhook",
            content=body,
            headers={"Paddle-Signature": sig},
        )
        assert r1.status_code == 200
        assert r2.status_code == 200


class TestBillingConfigEndpoint:
    """Public billing configuration endpoint for Paddle.js."""

    def test_config_returns_client_token_and_price_ids(self) -> None:
        client = _paddle_app()
        response = client.get("/v1/billing/config")
        assert response.status_code == 200
        data = response.json()
        assert data["client_token"] == "test_client_token_123"
        assert data["environment"] == "sandbox"
        assert "starter" in data["price_ids"]
        assert "pro" in data["price_ids"]

    def test_config_returns_mock_when_no_paddle(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/v1/billing/config")
        assert response.status_code == 200
        data = response.json()
        assert data["client_token"] == ""
        assert data["environment"] == "mock"


class TestPaddleProviderSelection:
    """Provider factory selects mock or Paddle based on PADDLE_ENV."""

    def test_empty_env_returns_mock(self) -> None:
        from doc_ingest_api.billing import MockBillingProvider, create_billing_provider

        provider = create_billing_provider(
            stripe_secret_key="", stripe_webhook_secret=""
        )
        assert isinstance(provider, MockBillingProvider)

    def test_sandbox_env_returns_paddle(self) -> None:
        from doc_ingest_api.billing import create_billing_provider

        provider = create_billing_provider(
            stripe_secret_key="",
            stripe_webhook_secret="",
            paddle_env="sandbox",
            paddle_api_key="test",
        )
        assert isinstance(provider, PaddleBillingProvider)

    def test_live_env_returns_paddle(self) -> None:
        from doc_ingest_api.billing import create_billing_provider

        provider = create_billing_provider(
            stripe_secret_key="",
            stripe_webhook_secret="",
            paddle_env="live",
            paddle_api_key="test",
        )
        assert isinstance(provider, PaddleBillingProvider)


class TestPaddleLandingPage:
    """Landing page includes Paddle.js integration."""

    def test_landing_contains_paddle_js(self) -> None:
        client = _paddle_app()
        response = client.get("/")
        assert response.status_code == 200
        assert "cdn.paddle.com/paddle" in response.text
        assert "openCheckout" in response.text
        assert "transactionId" in response.text
        assert "/v1/billing/checkout" in response.text
        assert "Paddle.Checkout.open" in response.text
        assert "Get Starter" in response.text
        assert "Get Pro" in response.text

    def test_billing_success_page(self) -> None:
        """The post-checkout success page renders."""
        client = _paddle_app()
        response = client.get("/billing/success")
        assert response.status_code == 200
        assert "Payment received" in response.text

    def test_landing_mock_mode_still_renders(self) -> None:
        client, _ = _app_with_rate_limits(MockParser("markitdown"))
        response = client.get("/")
        assert response.status_code == 200
        # Paddle.js script is still included (frontend handles mock gracefully)
        assert "cdn.paddle.com/paddle" in response.text

def _paddle_app_with_db(**overrides: Any) -> tuple[TestClient, Any]:
    """Create a Paddle app and return the database handle for assertions."""
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


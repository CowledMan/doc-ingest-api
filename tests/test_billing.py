"""Tests for the mock billing provider and billing API routes."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from doc_ingest_api.app import create_app
from doc_ingest_api.billing import (
    MockBillingProvider,
    create_billing_provider,
)
from doc_ingest_api.config import Settings
from doc_ingest_api.database import create_database
from doc_ingest_api.security import create_api_key


def _billing_app(**overrides: Any) -> tuple[TestClient, str]:
    """Create a test app with billing routes and return (client, api_key)."""
    settings = Settings(
        database_url="sqlite://",
        queue_backend="background",
        stripe_api_key="",
        stripe_webhook_secret="",
        **overrides,
    )
    database = create_database(settings.database_url)
    database.create_all()
    api_key = create_api_key(database.session_factory, name="billing-test")
    app = create_app(settings, session_factory=database.session_factory)
    return TestClient(app), api_key


class TestMockBillingProvider:
    """Unit tests for MockBillingProvider methods."""

    def test_create_checkout_session(self) -> None:
        provider = MockBillingProvider()
        result = provider.create_checkout_session(
            customer_id="cus_test_1234",
            price_id="price_test_starter",
            success_url="https://example.test/success",
            cancel_url="https://example.test/cancel",
        )
        assert result.id.startswith("cs_test_")
        assert result.url.startswith("https://checkout.stripe.com/")
        assert result.customer_id == "cus_test_1234"
        assert result.price_id == "price_test_starter"
        assert result.provider == "mock"

    def test_verify_webhook_signature_present(self) -> None:
        provider = MockBillingProvider()
        result = provider.verify_webhook_signature(
            payload=b"{}",
            signature_header="t=123,v1=abc",
            secret="whsec_test",
        )
        assert result.verified is True
        assert result.event_type == "checkout.session.completed"
        assert result.provider == "mock"

    def test_verify_webhook_signature_missing(self) -> None:
        provider = MockBillingProvider()
        result = provider.verify_webhook_signature(
            payload=b"{}",
            signature_header="",
            secret="whsec_test",
        )
        assert result.verified is False

    def test_get_subscription(self) -> None:
        provider = MockBillingProvider()
        result = provider.get_subscription("sub_test_1234")
        assert result.id == "sub_test_1234"
        assert result.status == "active"
        assert result.plan_name == "starter"
        assert result.monthly_page_quota == 500
        assert result.provider == "mock"


class TestBillingRoutes:
    """Integration tests for billing API endpoints."""

    def test_checkout_returns_mock_session(self) -> None:
        client, api_key = _billing_app()
        response = client.post(
            "/v1/billing/checkout",
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_provider"] == "mock"
        assert data["checkout_session_id"].startswith("cs_test_")
        assert data["checkout_url"].startswith("https://checkout.stripe.com/")

    def test_webhook_with_signature_returns_200(self) -> None:
        client, _api_key = _billing_app()
        response = client.post(
            "/v1/billing/webhook",
            headers={"Stripe-Signature": "t=123,v1=abc"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_provider"] == "mock"
        assert data["verified"] is True

    def test_webhook_missing_signature_returns_400(self) -> None:
        client, _api_key = _billing_app()
        response = client.post("/v1/billing/webhook")
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "missing_signature"

    def test_subscription_returns_active(self) -> None:
        client, api_key = _billing_app()
        response = client.get(
            "/v1/billing/subscription",
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["billing_provider"] == "mock"
        assert data["status"] == "active"
        assert data["plan_name"] == "starter"
        assert data["monthly_page_quota"] == 500

    def test_billing_requires_api_key(self) -> None:
        client, _ = _billing_app()
        response = client.get("/v1/billing/subscription")
        assert response.status_code == 401

    def test_real_stripe_key_raises_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError, match="Paddle"):
            create_billing_provider(
                stripe_secret_key="sk_test_real",
                stripe_webhook_secret="whsec_test",
            )

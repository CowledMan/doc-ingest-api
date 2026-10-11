"""Stripe billing provider protocol and mock implementation.

This module defines the billing interface used by the API gateway. The mock
implementation returns deterministic data shaped like real Stripe API
responses so the transition to a real Stripe integration in M2 is minimal.
No ``stripe`` pip dependency is introduced; the provider is selected based on
environment variables alone.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CheckoutSessionResult:
    """A checkout session response shaped like Stripe's ``Session`` object."""

    id: str
    url: str
    customer_id: str
    price_id: str
    provider: str


@dataclass(frozen=True)
class WebhookVerificationResult:
    """Result of checking a webhook signature header."""

    verified: bool
    event_type: str
    provider: str


@dataclass(frozen=True)
class SubscriptionResult:
    """A subscription response shaped like Stripe's ``Subscription`` object."""

    id: str
    customer_id: str
    status: str
    plan_name: str
    monthly_page_quota: int
    provider: str


class BillingProvider(Protocol):
    """Protocol that all billing backends must satisfy.

    The method signatures mirror the subset of the real Stripe API needed for
    checkout, webhook verification, and subscription status queries.
    """

    provider_name: str

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        api_key_id: int | None = None,
    ) -> CheckoutSessionResult:
        """Create a hosted checkout session and return its URL."""
        ...

    def verify_webhook_signature(
        self,
        *,
        payload: bytes,
        signature_header: str,
        secret: str,
    ) -> WebhookVerificationResult:
        """Verify a webhook signature and return the parsed event type."""
        ...

    def get_subscription(self, subscription_id: str) -> SubscriptionResult:
        """Return subscription status and plan details."""
        ...

    def create_portal_session(self, customer_id: str) -> str:
        """Create a customer portal session and return the URL."""
        ...


class MockBillingProvider:
    """Deterministic billing provider for local development and testing.

    Every method returns simulated data with realistic Stripe-like ID shapes
    (``cs_test_...``, ``sub_test_...``) so downstream code can be written
    against the same interface without a real Stripe account.
    """

    def __init__(self, *, webhook_secret: str = "") -> None:
        """Create a mock provider with an optional webhook secret."""
        self.provider_name = "mock"
        self._webhook_secret = webhook_secret

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        api_key_id: int | None = None,
    ) -> CheckoutSessionResult:
        """Return a fake checkout session with a non-functional URL."""
        del success_url, cancel_url, api_key_id
        session_id = f"cs_test_{hashlib.sha256(customer_id.encode()).hexdigest()[:24]}"
        return CheckoutSessionResult(
            id=session_id,
            url=f"https://checkout.stripe.com/c/pay/{session_id}",
            customer_id=customer_id,
            price_id=price_id,
            provider=self.provider_name,
        )

    def verify_webhook_signature(
        self,
        *,
        payload: bytes,
        signature_header: str,
        secret: str,
    ) -> WebhookVerificationResult:
        """Check that a signature header exists; mock mode never rejects."""
        del payload, secret
        verified = bool(signature_header)
        event_type = "checkout.session.completed"
        return WebhookVerificationResult(
            verified=verified,
            event_type=event_type,
            provider=self.provider_name,
        )

    def get_subscription(self, subscription_id: str) -> SubscriptionResult:
        """Return an active subscription with the default starter plan."""
        return SubscriptionResult(
            id=subscription_id,
            customer_id=f"cus_test_{hashlib.sha256(subscription_id.encode()).hexdigest()[:14]}",
            status="active",
            plan_name="starter",
            monthly_page_quota=500,
            provider=self.provider_name,
        )

    def create_portal_session(self, customer_id: str) -> str:
        """Return a fake portal URL for mock mode."""
        return f"https://sandbox-portal.paddle.com/mock/{customer_id}"


def create_billing_provider(
    *,
    stripe_secret_key: str,
    stripe_webhook_secret: str,
    paddle_env: str = "",
    paddle_api_key: str = "",
    paddle_webhook_secret: str = "",
    paddle_client_token: str = "",
) -> BillingProvider:
    """Return the appropriate billing provider based on configuration.

    When ``paddle_env`` is empty, a ``MockBillingProvider`` is returned
    (existing M1/M2 batch 1-2 behaviour unchanged). When set to ``sandbox``
    or ``live``, a ``PaddleBillingProvider`` is used.
    """
    if paddle_env in ("sandbox", "live"):
        from .paddle_billing import PaddleBillingProvider

        return PaddleBillingProvider(
            paddle_env=paddle_env,
            api_key=paddle_api_key,
            webhook_secret=paddle_webhook_secret,
            client_token=paddle_client_token,
        )
    if stripe_secret_key:
        raise NotImplementedError(
            "Real Stripe integration is not supported; use Paddle (PADDLE_ENV)"
        )
    return MockBillingProvider(webhook_secret=stripe_webhook_secret)


def billing_response(result: Any) -> dict[str, Any]:
    """Wrap a billing result dict with the ``billing_provider`` marker."""
    payload = dict(result)
    payload.setdefault("billing_provider", "mock")
    return payload

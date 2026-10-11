"""Paddle billing provider implementing the ``BillingProvider`` protocol.

Uses the Paddle REST API directly via ``httpx`` (already a project dependency)
to avoid introducing the Apache-2.0 ``paddle-python`` SDK. The interface
matches the shared ``BillingProvider`` protocol so the gateway can swap
providers without changing route handlers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx

from .billing import (
    CheckoutSessionResult,
    SubscriptionResult,
    WebhookVerificationResult,
)

PADDLE_SANDBOX_URL = "https://sandbox-api.paddle.com"
PADDLE_LIVE_URL = "https://api.paddle.com"


class PaddleAPIError(Exception):
    """Raised when the Paddle API returns an unexpected error."""

    def __init__(self, status_code: int, body: str) -> None:
        """Store the HTTP status and response body."""
        super().__init__(f"Paddle API error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class PaddleBillingProvider:
    """Paddle REST API billing provider.

    Uses ``httpx`` for API calls. Supports sandbox and live environments via
    ``paddle_env`` (``sandbox`` or ``live``). API key and webhook secret are
    read from environment variables, never hardcoded.
    """

    def __init__(
        self,
        *,
        paddle_env: str,
        api_key: str,
        webhook_secret: str = "",
        client_token: str = "",
    ) -> None:
        """Create a Paddle provider for the given environment."""
        self.provider_name = "paddle"
        self.paddle_env = paddle_env
        self.base_url = (
            PADDLE_SANDBOX_URL if paddle_env == "sandbox" else PADDLE_LIVE_URL
        )
        if not api_key:
            raise ValueError(
                "Paddle billing is enabled but no API key is configured. "
                "Set PADDLE_API_KEY (or PADDLE_SANDBOX_API_KEY for local sandbox use)."
            )
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._client_token = client_token

    def _headers(self) -> dict[str, str]:
        """Return authentication headers for Paddle API calls."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        api_key_id: int | None = None,
    ) -> CheckoutSessionResult:
        """Create a Paddle transaction (checkout session)."""
        custom_data: dict[str, Any] = {"customer_id": customer_id}
        if api_key_id is not None:
            # Webhooks echo custom_data back on subscription events, letting
            # us map an incoming subscription to the local API key.
            custom_data["api_key_id"] = str(api_key_id)
        payload: dict[str, Any] = {
            "items": [{"price_id": price_id, "quantity": 1}],
            "custom_data": custom_data,
        }
        response = httpx.post(
            f"{self.base_url}/transactions",
            headers=self._headers(),
            json=payload,
            timeout=30.0,
        )
        if response.status_code != 201:
            raise PaddleAPIError(response.status_code, response.text)
        body = response.json()
        data = body.get("data", {})
        txn_id = data.get("id", "")
        # Paddle checkout URL is constructed from the transaction ID.
        checkout_url = f"{success_url}?_ptxn={txn_id}"
        return CheckoutSessionResult(
            id=txn_id,
            url=checkout_url,
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
        """Verify Paddle webhook signature (HMAC-SHA256 over ``ts:body``).

        Paddle sends the signature in the ``Paddle-Signature`` header with the
        format ``ts=<unix_timestamp>;h1=<hex_hmac>``. The HMAC is computed
        over ``<timestamp>:<raw_body>`` using the webhook secret as key.
        """
        if not signature_header or not secret:
            return WebhookVerificationResult(
                verified=False,
                event_type="",
                provider=self.provider_name,
            )

        # Parse ts and h1 from the signature header
        parts = dict(
            piece.split("=", 1) for piece in signature_header.split(";")
        )
        timestamp = parts.get("ts", "")
        received_hmac = parts.get("h1", "")
        if not timestamp or not received_hmac:
            return WebhookVerificationResult(
                verified=False,
                event_type="",
                provider=self.provider_name,
            )

        # Compute expected HMAC over "<timestamp>:<body>"
        signed_payload = f"{timestamp}:{payload.decode('utf-8', errors='replace')}"
        expected_hmac = hmac.new(
            secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected_hmac, received_hmac):
            return WebhookVerificationResult(
                verified=False,
                event_type="",
                provider=self.provider_name,
            )

        # Parse event type from the body
        try:
            event = json.loads(payload.decode("utf-8"))
            event_type = event.get("event_type", "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            event_type = ""
        return WebhookVerificationResult(
            verified=True,
            event_type=event_type,
            provider=self.provider_name,
        )

    def get_subscription(self, subscription_id: str) -> SubscriptionResult:
        """Fetch subscription status from Paddle API."""
        response = httpx.get(
            f"{self.base_url}/subscriptions/{subscription_id}",
            headers=self._headers(),
            timeout=30.0,
        )
        if response.status_code != 200:
            raise PaddleAPIError(response.status_code, response.text)
        body = response.json()
        data = body.get("data", {})
        items = data.get("items", [])
        plan_name = "free"
        monthly_quota = 100
        if items:
            custom = items[0].get("price", {}).get("custom_data") or {}
            plan_name = str(custom.get("plan", "free"))
            monthly_quota = int(custom.get("monthly_pages", 100))
        return SubscriptionResult(
            id=subscription_id,
            customer_id=data.get("customer_id", ""),
            status=data.get("status", "unknown"),
            plan_name=plan_name,
            monthly_page_quota=monthly_quota,
            provider=self.provider_name,
        )

    def create_portal_session(self, customer_id: str) -> str:
        """Create a Paddle customer portal session and return the URL."""
        response = httpx.post(
            f"{self.base_url}/customers/{customer_id}/portal-sessions",
            headers=self._headers(),
            json={},
            timeout=30.0,
        )
        if response.status_code not in (200, 201):
            raise PaddleAPIError(response.status_code, response.text)
        body = response.json()
        urls = body.get("data", {}).get("urls", {})
        return str(urls.get("general", {}).get("overview", ""))

    def get_config(self) -> dict[str, Any]:
        """Return public billing configuration for the frontend."""
        return {
            "client_token": self._client_token,
            "environment": self.paddle_env,
        }


def verify_paddle_webhook_signature(
    *,
    raw_body: bytes,
    signature_header: str,
    secret: str,
) -> bool:
    """Standalone webhook signature verification for direct use in routes."""
    if not signature_header or not secret:
        return False
    parts = dict(
        piece.split("=", 1) for piece in signature_header.split(";")
    )
    timestamp = parts.get("ts", "")
    received_hmac = parts.get("h1", "")
    if not timestamp or not received_hmac:
        return False
    signed_payload = f"{timestamp}:{raw_body.decode('utf-8', errors='replace')}"
    expected_hmac = hmac.new(
        secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_hmac, received_hmac)

"""Idempotent Paddle sandbox catalog seed script.

Creates Starter ($19/mo) and Pro ($99/mo) products in the Paddle sandbox
if they do not already exist. Safe to run multiple times.
"""

from __future__ import annotations

import os
import sys

import httpx

PADDLE_SANDBOX_URL = "https://sandbox-api.paddle.com"


def get_api_key() -> str:
    """Read the Paddle API key from environment variables."""
    key = os.environ.get("PADDLE_API_KEY") or os.environ.get("PADDLE_SANDBOX_API_KEY")
    if not key:
        print("Error: set PADDLE_API_KEY or PADDLE_SANDBOX_API_KEY", file=sys.stderr)
        sys.exit(1)
    return key


def headers(api_key: str) -> dict[str, str]:
    """Return auth headers for Paddle API."""
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def find_or_create_product(
    client: httpx.Client,
    api_key: str,
    name: str,
    description: str,
    plan: str,
    monthly_pages: int,
) -> str:
    """Find an existing product by name or create a new one."""
    response = client.get(
        f"{PADDLE_SANDBOX_URL}/products",
        headers=headers(api_key),
    )
    response.raise_for_status()
    for product in response.json().get("data", []):
        if product["name"] == name:
            print(f"  product exists: {name} ({product['id']})")
            return str(product["id"])

    response = client.post(
        f"{PADDLE_SANDBOX_URL}/products",
        headers=headers(api_key),
        json={
            "name": name,
            "description": description,
            "type": "standard",
            "tax_category": "saas",
            "custom_data": {"plan": plan, "monthly_pages": monthly_pages},
        },
    )
    response.raise_for_status()
    product = response.json()["data"]
    print(f"  product created: {name} ({product['id']})")
    return str(product["id"])


def find_or_create_price(
    client: httpx.Client,
    api_key: str,
    product_id: str,
    name: str,
    description: str,
    amount_cents: int,
    plan: str,
    monthly_pages: int,
) -> str:
    """Find an existing price or create a recurring monthly price."""
    response = client.get(
        f"{PADDLE_SANDBOX_URL}/prices",
        headers=headers(api_key),
        params={"include": ""},
    )
    response.raise_for_status()
    for price in response.json().get("data", []):
        if price.get("name") == name:
            print(f"  price exists: {name} ({price['id']})")
            return str(price["id"])

    response = client.post(
        f"{PADDLE_SANDBOX_URL}/prices",
        headers=headers(api_key),
        json={
            "description": description,
            "name": name,
            "product_id": product_id,
            "billing_cycle": {"interval": "month", "frequency": 1},
            "unit_price": {"amount": str(amount_cents), "currency_code": "USD"},
            "custom_data": {"plan": plan, "monthly_pages": monthly_pages},
        },
    )
    response.raise_for_status()
    price = response.json()["data"]
    print(f"  price created: {name} ({price['id']})")
    return str(price["id"])


def main() -> None:
    """Seed the Paddle sandbox catalog."""
    api_key = get_api_key()
    with httpx.Client(timeout=30.0) as client:
        print("Seeding Paddle sandbox catalog...")

        starter_pid = find_or_create_product(
            client, api_key,
            "Document Ingest API - Starter",
            "500 pages per month of document parsing",
            "starter", 500,
        )
        starter_prid = find_or_create_price(
            client, api_key, starter_pid,
            "Starter Monthly", "Starter plan monthly billing",
            1900, "starter", 500,
        )

        pro_pid = find_or_create_product(
            client, api_key,
            "Document Ingest API - Pro",
            "5000 pages per month of document parsing",
            "pro", 5000,
        )
        pro_prid = find_or_create_price(
            client, api_key, pro_pid,
            "Pro Monthly", "Pro plan monthly billing",
            9900, "pro", 5000,
        )

        print(f"\n  PADDLE_PRICE_STARTER={starter_prid}")
        print(f"  PADDLE_PRICE_PRO={pro_prid}")
        print("\nDone. Set these as environment variables in production.")


if __name__ == "__main__":
    main()

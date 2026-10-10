"""API key generation, hashing, and lookup helpers."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import ApiKey

API_KEY_PREFIX = "dip_"


def generate_api_key() -> str:
    """Generate a new bearer-style API key."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    """Return the deterministic SHA256 hash used for API key lookup."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def create_api_key(
    session_factory: sessionmaker[Session],
    *,
    name: str,
    monthly_page_quota: int = 500,
) -> str:
    """Create an API key row and return the unhashed key exactly once."""
    if monthly_page_quota < 0:
        raise ValueError("monthly_page_quota must be non-negative")
    raw_key = generate_api_key()
    with session_factory.begin() as session:
        session.add(
            ApiKey(
                name=name,
                key_hash=hash_api_key(raw_key),
                monthly_page_quota=monthly_page_quota,
                used_pages=0,
                active=True,
            )
        )
    return raw_key


def find_api_key(session: Session, raw_key: str) -> ApiKey | None:
    """Find an active API key by its unhashed value."""
    return session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw_key)))


def serialize_api_key(api_key: ApiKey) -> dict[str, Any]:
    """Return safe, non-secret API key metadata for CLI output."""
    return {
        "id": api_key.id,
        "name": api_key.name,
        "active": api_key.active,
        "monthly_page_quota": api_key.monthly_page_quota,
        "used_pages": api_key.used_pages,
        "created_at": api_key.created_at.isoformat(),
    }

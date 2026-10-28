"""API key generation, hashing, and lookup helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
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


SESSION_COOKIE_NAME = "doingest_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600
PBKDF2_ITERATIONS = 240_000
FREE_PLAN_QUOTA = 5
KEY_PREFIX_LENGTH = 12


def key_prefix(raw_key: str) -> str:
    """Return the displayable prefix of an API key (never the secret)."""
    return raw_key[:KEY_PREFIX_LENGTH]


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-SHA256; returns a self-describing string."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify a password against a stored PBKDF2 hash in constant time."""
    try:
        algo, iterations_raw, salt_hex, digest_hex = stored.split("$")
        iterations = int(iterations_raw)
    except (ValueError, TypeError):
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


def sign_session(user_id: int, secret: str) -> str:
    """Create a signed session token for one user (30-day TTL)."""
    expires = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{user_id}.{expires}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_session(token: str, secret: str) -> int | None:
    """Validate a signed session token and return the user id, or None."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    user_id_raw, expires_raw, signature = parts
    payload = f"{user_id_raw}.{expires_raw}"
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        expires = int(expires_raw)
        user_id = int(user_id_raw)
    except ValueError:
        return None
    if expires < time.time():
        return None
    return user_id


def create_user_api_key(
    session_factory: sessionmaker[Session],
    *,
    user_id: int,
    name: str = "default",
) -> dict[str, str]:
    """Create a non-expiring free-plan API key owned by one user."""
    if not name or len(name) > 120:
        name = "default"
    raw_key = generate_api_key()
    with session_factory.begin() as session:
        session.add(
            ApiKey(
                name=name,
                key_hash=hash_api_key(raw_key),
                key_prefix=key_prefix(raw_key),
                monthly_page_quota=FREE_PLAN_QUOTA,
                used_pages=0,
                active=True,
                plan="free",
                owner_user_id=user_id,
            )
        )
    return {"api_key": raw_key, "key_prefix": key_prefix(raw_key)}

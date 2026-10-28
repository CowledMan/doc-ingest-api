"""Tests for user accounts, auth, dashboard key management."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from doc_ingest_api.app import create_app
from doc_ingest_api.config import Settings
from doc_ingest_api.database import Database, create_database
from doc_ingest_api.security import create_api_key


def _settings(**overrides: Any) -> Settings:
    """Create isolated auth settings."""
    values: dict[str, Any] = {
        "database_url": "sqlite://",
        "queue_backend": "background",
        "parser_timeout_seconds": 0.01,
        "app_secret_key": "test-secret",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def auth_client() -> TestClient:
    """App with in-memory SQLite for auth tests."""
    settings = _settings()
    database: Database = create_database(settings.database_url)
    database.create_all()
    app: FastAPI = create_app(settings, session_factory=database.session_factory)
    with TestClient(app) as client:
        yield client


def _register(client: TestClient, email: str = "user@example.com", password: str = "hunter2boogaloo") -> Any:
    return client.post(
        "/api/auth/register",
        json={"email": email, "password": password},
    )


class TestRegistration:
    def test_register_and_cookie(self, auth_client: TestClient) -> None:
        res = _register(auth_client)
        assert res.status_code == 200
        assert res.json()["user"]["email"] == "user@example.com"
        assert "doingest_session" in auth_client.cookies

    def test_duplicate_email_conflict(self, auth_client: TestClient) -> None:
        assert _register(auth_client).status_code == 200
        res = _register(auth_client, password="another-pass-1")
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "email_exists"

    def test_weak_password_rejected(self, auth_client: TestClient) -> None:
        res = _register(auth_client, password="short")
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "weak_password"

    def test_invalid_email_rejected(self, auth_client: TestClient) -> None:
        res = _register(auth_client, email="not-an-email")
        assert res.status_code == 422


class TestLogin:
    def test_login_success(self, auth_client: TestClient) -> None:
        _register(auth_client)
        res = auth_client.post(
            "/api/auth/login", json={"email": "user@example.com", "password": "hunter2boogaloo"}
        )
        assert res.status_code == 200

    def test_login_wrong_password(self, auth_client: TestClient) -> None:
        _register(auth_client)
        res = auth_client.post(
            "/api/auth/login", json={"email": "user@example.com", "password": "wrong-password"}
        )
        assert res.status_code == 401

    def test_login_unknown_email(self, auth_client: TestClient) -> None:
        res = auth_client.post(
            "/api/auth/login", json={"email": "ghost@example.com", "password": "whatever-pass"}
        )
        assert res.status_code == 401

    def test_logout_clears_session(self, auth_client: TestClient) -> None:
        _register(auth_client)
        assert auth_client.get("/api/keys").status_code == 200
        auth_client.post("/api/auth/logout")
        res = auth_client.get("/api/keys")
        assert res.status_code == 401
        assert res.json()["detail"]["code"] == "login_required"


class TestDashboardPage:
    def test_requires_login(self, auth_client: TestClient) -> None:
        res = auth_client.get("/dashboard", follow_redirects=False)
        assert res.status_code == 303
        assert res.headers["location"] == "/login"

    def test_served_when_logged_in(self, auth_client: TestClient) -> None:
        _register(auth_client)
        res = auth_client.get("/dashboard")
        assert res.status_code == 200
        assert "Dashboard" in res.text

    def test_login_page_public(self, auth_client: TestClient) -> None:
        res = auth_client.get("/login")
        assert res.status_code == 200
        assert "Sign in" in res.text


class TestKeyManagement:
    def test_create_key_returns_raw_once(self, auth_client: TestClient) -> None:
        _register(auth_client)
        res = auth_client.post("/api/keys", json={"name": "prod"})
        assert res.status_code == 200
        data = res.json()
        assert data["api_key"].startswith("dip_")
        assert data["plan"] == "free"
        assert data["monthly_page_quota"] == 5

        listed = auth_client.get("/api/keys").json()["keys"]
        assert len(listed) == 1
        entry = listed[0]
        assert entry["name"] == "prod"
        assert entry["key_prefix"] == data["api_key"][:12]
        assert data["api_key"] not in str(listed)

    def test_keys_scoped_per_user(self, auth_client: TestClient) -> None:
        _register(auth_client, email="a@example.com")
        auth_client.post("/api/keys", json={"name": "a-key"})
        auth_client.post("/api/auth/logout")
        _register(auth_client, email="b@example.com")
        keys = auth_client.get("/api/keys").json()["keys"]
        assert all(k["name"] != "a-key" for k in keys)

    def test_revoke_key(self, auth_client: TestClient) -> None:
        _register(auth_client)
        raw = auth_client.post("/api/keys", json={"name": "k"}).json()["api_key"]
        key_id = auth_client.get("/api/keys").json()["keys"][0]["id"]

        res = auth_client.post(f"/api/keys/{key_id}/revoke")
        assert res.status_code == 200

        listed = auth_client.get("/api/keys").json()["keys"]
        assert listed[0]["active"] is False

        # Revoked key must fail authentication on the parse endpoint.
        parse_res = auth_client.post(
            "/v1/parse",
            headers={"X-API-Key": raw},
            files={"file": ("a.txt", b"hello", "text/plain")},
        )
        assert parse_res.status_code == 401

    def test_revoke_other_users_key_404(self, auth_client: TestClient) -> None:
        _register(auth_client, email="a@example.com")
        auth_client.post("/api/keys", json={"name": "a-key"})
        key_id = auth_client.get("/api/keys").json()["keys"][0]["id"]
        auth_client.post("/api/auth/logout")

        _register(auth_client, email="b@example.com")
        res = auth_client.post(f"/api/keys/{key_id}/revoke")
        assert res.status_code == 404

    def test_dashboard_checkout_with_session(self, auth_client: TestClient) -> None:
        """Session-auth checkout creates a mock checkout for an owned key."""
        _register(auth_client)
        auth_client.post("/api/keys", json={"name": "k"})
        key_id = auth_client.get("/api/keys").json()["keys"][0]["id"]
        res = auth_client.post(
            "/api/billing/checkout", json={"key_id": key_id, "plan": "starter"}
        )
        assert res.status_code == 200
        assert "checkout_session_id" in res.json()

    def test_dashboard_checkout_rejects_foreign_key(self, auth_client: TestClient) -> None:
        _register(auth_client, email="a@example.com")
        auth_client.post("/api/keys", json={"name": "a-key"})
        key_id = auth_client.get("/api/keys").json()["keys"][0]["id"]
        auth_client.post("/api/auth/logout")

        _register(auth_client, email="b@example.com")
        res = auth_client.post(
            "/api/billing/checkout", json={"key_id": key_id, "plan": "starter"}
        )
        assert res.status_code == 404


class TestDemoFlowUnaffected:
    def test_demo_keys_still_work(self, auth_client: TestClient) -> None:
        res = auth_client.post("/v1/demo-keys", json={"email": "d@example.com"})
        assert res.status_code == 200
        assert res.json()["api_key"].startswith("dip_")

    def test_legacy_email_passwordless_recovery_untouched(
        self, auth_client: TestClient
    ) -> None:
        """Old email-recovery flow remains available alongside accounts."""
        res = auth_client.post("/v1/keys/recover", json={"email": "nobody@example.com"})
        assert res.status_code == 404


def test_migration_adds_columns_to_existing_db(tmp_path: Any) -> None:
    """An existing api_keys table gains key_prefix/owner_user_id on startup."""
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE api_keys (id INTEGER PRIMARY KEY, name VARCHAR(120), "
        "key_hash VARCHAR(64), monthly_page_quota INTEGER, used_pages INTEGER, "
        "active BOOLEAN, plan VARCHAR(32), expires_at TIMESTAMP, "
        "paddle_customer_id VARCHAR(64), owner_email VARCHAR(255), "
        "period_start DATE, created_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO api_keys (name, key_hash, monthly_page_quota, used_pages, active, plan) "
        "VALUES ('legacy', 'x', 5, 0, 1, 'free')"
    )
    conn.commit()
    conn.close()

    settings = _settings(database_url=f"sqlite:///{db_path}")
    database = create_database(settings.database_url)
    database.create_all()

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "key_prefix" in cols
    assert "owner_user_id" in cols
    assert "users" in tables

    # create_api_key (legacy helper) still works against the migrated table.
    raw = create_api_key(database.session_factory, name="after-migration")
    assert raw.startswith("dip_")


"""Tests for dashboard v2: usage summary, account management, sync job records."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from doc_ingest_api.app import create_app
from doc_ingest_api.config import Settings
from doc_ingest_api.database import Database, create_database


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "sqlite://",
        "queue_backend": "background",
        "parser_timeout_seconds": 0.01,
        "app_secret_key": "test-secret",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def client() -> TestClient:
    settings = _settings()
    database: Database = create_database(settings.database_url)
    database.create_all()
    app: FastAPI = create_app(settings, session_factory=database.session_factory)
    with TestClient(app) as test_client:
        yield test_client


def _register(client: TestClient, email: str = "v2@example.com") -> None:
    res = client.post("/api/auth/register", json={"email": email, "password": "hunter2boogaloo"})
    assert res.status_code == 200


class TestUsageSummary:
    def test_requires_login(self, client: TestClient) -> None:
        res = client.get("/api/usage/summary")
        assert res.status_code == 401

    def test_empty_summary_without_keys(self, client: TestClient) -> None:
        _register(client)
        res = client.get("/api/usage/summary")
        assert res.status_code == 200
        data = res.json()
        assert data["days"] == 30
        assert len(data["daily"]) == 30
        assert data["totals"] == {"pages": 0, "requests": 0, "succeeded": 0, "failed": 0}
        assert data["recent"] == []

    def test_days_parameter_is_clamped(self, client: TestClient) -> None:
        _register(client)
        assert client.get("/api/usage/summary?days=400").json()["days"] == 90
        assert client.get("/api/usage/summary?days=1").json()["days"] == 7


class TestAccountEndpoints:
    def test_account_info(self, client: TestClient) -> None:
        _register(client)
        res = client.get("/api/account")
        assert res.status_code == 200
        data = res.json()
        assert data["email"] == "v2@example.com"
        assert data["email_verified"] is False
        assert data["created_at"]

    def test_account_requires_login(self, client: TestClient) -> None:
        assert client.get("/api/account").status_code == 401

    def test_change_password_success(self, client: TestClient) -> None:
        _register(client)
        res = client.post(
            "/api/account/password",
            json={"current_password": "hunter2boogaloo", "new_password": "newer3secret"},
        )
        assert res.status_code == 200
        client.post("/api/auth/logout")
        assert (
            client.post(
                "/api/auth/login",
                json={"email": "v2@example.com", "password": "newer3secret"},
            ).status_code
            == 200
        )

    def test_change_password_wrong_current(self, client: TestClient) -> None:
        _register(client)
        res = client.post(
            "/api/account/password",
            json={"current_password": "wrong-password", "new_password": "newer3secret"},
        )
        assert res.status_code == 401

    def test_change_password_too_short(self, client: TestClient) -> None:
        _register(client)
        res = client.post(
            "/api/account/password",
            json={"current_password": "hunter2boogaloo", "new_password": "short"},
        )
        assert res.status_code == 422


class TestDashboardSubpages:
    def test_pages_require_login(self, client: TestClient) -> None:
        for path in ("/dashboard", "/dashboard/keys", "/dashboard/usage", "/dashboard/account"):
            res = client.get(path, follow_redirects=False)
            assert res.status_code == 303
            assert res.headers["location"] == "/login"

    def test_pages_render_when_logged_in(self, client: TestClient) -> None:
        _register(client)
        expected = {
            "/dashboard": "Overview",
            "/dashboard/keys": "API Keys",
            "/dashboard/usage": "Pages per day",
            "/dashboard/account": "Change password",
        }
        for path, marker in expected.items():
            res = client.get(path)
            assert res.status_code == 200, path
            assert marker in res.text, path
            assert 'class="tabs"' in res.text


class TestKeyLimit:
    def test_active_key_limit(self, client: TestClient) -> None:
        _register(client)
        for i in range(3):
            res = client.post("/api/keys", json={"name": f"key-{i}"})
            assert res.status_code == 200, i
        res = client.post("/api/keys", json={"name": "key-overflow"})
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "key_limit_reached"

    def test_revoked_keys_do_not_count(self, client: TestClient) -> None:
        _register(client)
        for i in range(3):
            assert client.post("/api/keys", json={"name": f"key-{i}"}).status_code == 200
        keys = client.get("/api/keys").json()["keys"]
        revoke_id = keys[0]["id"]
        assert client.post(f"/api/keys/{revoke_id}/revoke").status_code == 200
        res = client.post("/api/keys", json={"name": "after-revoke"})
        assert res.status_code == 200


class TestFirstKeyFreeAllowance:
    def test_only_first_active_key_is_free(self, client: TestClient) -> None:
        _register(client)
        first = client.post("/api/keys", json={"name": "first"}).json()
        assert first["monthly_page_quota"] == 5
        second = client.post("/api/keys", json={"name": "second"}).json()
        assert second["monthly_page_quota"] == 0
        third = client.post("/api/keys", json={"name": "third"}).json()
        assert third["monthly_page_quota"] == 0

    def test_free_allowance_returns_after_revoking(self, client: TestClient) -> None:
        _register(client)
        client.post("/api/keys", json={"name": "first"})
        keys = client.get("/api/keys").json()["keys"]
        client.post(f"/api/keys/{keys[0]['id']}/revoke")
        fresh = client.post("/api/keys", json={"name": "fresh"}).json()
        assert fresh["monthly_page_quota"] == 5


class TestDemoKeyAuth:
    def test_anonymous_rejected(self, client: TestClient) -> None:
        res = client.post("/v1/demo-keys")
        assert res.status_code == 401

    def test_one_active_demo_per_user(self, client: TestClient) -> None:
        _register(client)
        assert client.post("/v1/demo-keys").status_code == 200
        res = client.post("/v1/demo-keys")
        assert res.status_code == 409
        assert res.json()["detail"]["code"] == "demo_key_exists"


class TestPaidKeyProtection:
    def test_cannot_revoke_subscribed_key(self) -> None:
        settings = _settings()
        database = create_database(settings.database_url)
        database.create_all()
        app: FastAPI = create_app(settings, session_factory=database.session_factory)
        with TestClient(app) as test_client:
            _register(test_client, email="paid@example.com")
            assert test_client.post("/api/keys", json={"name": "paid"}).status_code == 200
            key_id = test_client.get("/api/keys").json()["keys"][0]["id"]
            from doc_ingest_api.models import ApiKey

            with database.session_factory.begin() as db:
                key = db.get(ApiKey, key_id)
                key.plan = "starter"
                key.monthly_page_quota = 500
            res = test_client.post(f"/api/keys/{key_id}/revoke")
            assert res.status_code == 409
            assert res.json()["detail"]["code"] == "subscription_active"
            # After the subscription ends (plan back to free), revoke works.
            with database.session_factory.begin() as db:
                key = db.get(ApiKey, key_id)
                key.plan = "free"
                key.monthly_page_quota = 5
            assert test_client.post(f"/api/keys/{key_id}/revoke").status_code == 200

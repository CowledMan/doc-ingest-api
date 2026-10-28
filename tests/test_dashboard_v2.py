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

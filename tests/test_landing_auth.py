"""Tests for auth-gated landing CTAs and session billing shortcuts."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from doc_ingest_api.app import create_app
from doc_ingest_api.config import Settings
from doc_ingest_api.database import create_database


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
    database = create_database(settings.database_url)
    database.create_all()
    app: FastAPI = create_app(settings, session_factory=database.session_factory)
    with TestClient(app) as c:
        yield c


def _register(client: TestClient, email: str = "me@example.com") -> None:
    res = client.post(
        "/api/auth/register", json={"email": email, "password": "hunter2boogaloo"}
    )
    assert res.status_code == 200


class TestAuthMe:
    def test_anonymous(self, client: TestClient) -> None:
        res = client.get("/api/auth/me")
        assert res.status_code == 200
        assert res.json() == {"authenticated": False}

    def test_logged_in(self, client: TestClient) -> None:
        _register(client)
        res = client.get("/api/auth/me")
        assert res.status_code == 200
        data = res.json()
        assert data["authenticated"] is True
        assert data["email"] == "me@example.com"


class TestCheckoutAutoKey:
    def test_checkout_without_key_id_creates_key(self, client: TestClient) -> None:
        _register(client)
        res = client.post("/api/billing/checkout", json={"plan": "starter"})
        assert res.status_code == 200
        keys = client.get("/api/keys").json()["keys"]
        assert len(keys) == 1
        assert keys[0]["name"] == "default"
        assert keys[0]["plan"] == "free"

    def test_checkout_reuses_existing_active_key(self, client: TestClient) -> None:
        _register(client)
        client.post("/api/keys", json={"name": "explicit"})
        res = client.post("/api/billing/checkout", json={"plan": "starter"})
        assert res.status_code == 200
        keys = client.get("/api/keys").json()["keys"]
        assert len(keys) == 1
        assert keys[0]["name"] == "explicit"


class TestLandingPage:
    def test_no_anonymous_key_inputs(self, client: TestClient) -> None:
        html = client.get("/").text
        assert "api-key-input" not in html
        assert "Email (for key recovery)" not in html
        assert "try-free" not in html

    def test_ctas_are_login_gated(self, client: TestClient) -> None:
        html = client.get("/").text
        assert 'onclick="startFree()"' in html
        assert 'onclick="openCheckout(' in html
        assert "requireLogin" in html
        assert "/api/auth/me" in html

    def test_nav_login_link_present(self, client: TestClient) -> None:
        html = client.get("/").text
        assert 'href="/login"' in html

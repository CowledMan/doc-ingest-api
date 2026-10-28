"""Tests for email binding on demo keys and key recovery flow."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from doc_ingest_api.config import Settings
from doc_ingest_api.database import create_database
from doc_ingest_api.models import ApiKey
from doc_ingest_api.security import hash_api_key
from tests.test_api import MockParser


def _app(**overrides: Any) -> tuple[TestClient, Any]:
    """Create a test app with tight IP limits for recover tests."""
    values: dict[str, Any] = {
        "database_url": "sqlite://",
        "queue_backend": "background",
        "paddle_env": "",
        "parser_timeout_seconds": 0.01,
        "confidence_threshold": 0.6,
        "monthly_page_quota": 500,
        **overrides,
    }
    settings = Settings(**values)
    database = create_database(settings.database_url)
    database.create_all()
    from doc_ingest_api.app import create_app

    parser = MockParser("markitdown")
    app = create_app(
        settings,
        session_factory=database.session_factory,
        parser_router=type("R", (), {"parse": lambda self, path, category=None: parser.parse(path)})(),
    )
    return TestClient(app), database


class TestOwnerEmailMigration:
    """owner_email column exists and is written on demo-keys with email."""

    def test_migration_is_idempotent(self) -> None:
        database = create_database("sqlite://")
        database.create_all()
        database.create_all()

    def test_demo_key_with_email_writes_owner(self) -> None:
        client, database = _app()
        response = client.post(
            "/v1/demo-keys",
            json={"email": "owner@example.com"},
        )
        assert response.status_code == 200
        raw_key = response.json()["api_key"]
        with database.session_factory() as s:
            key = (
                s.query(ApiKey)
                .filter(ApiKey.key_hash == hash_api_key(raw_key))
                .one()
            )
            assert key.owner_email == "owner@example.com"

    def test_demo_key_without_email_has_none(self) -> None:
        client, database = _app()
        response = client.post("/v1/demo-keys")
        assert response.status_code == 200
        raw_key = response.json()["api_key"]
        with database.session_factory() as s:
            key = (
                s.query(ApiKey)
                .filter(ApiKey.key_hash == hash_api_key(raw_key))
                .one()
            )
            assert key.owner_email is None

    def test_demo_key_invalid_email_returns_422(self) -> None:
        client, _db = _app()
        response = client.post("/v1/demo-keys", json={"email": "not-an-email"})
        assert response.status_code == 422


class TestKeyRecovery:
    """POST /v1/keys/recover issues new keys and deactivates old ones."""

    def test_recover_returns_new_keys(self) -> None:
        client, database = _app()
        raw_key = client.post(
            "/v1/demo-keys", json={"email": "recover@example.com"}
        ).json()["api_key"]
        response = client.post(
            "/v1/keys/recover", json={"email": "recover@example.com"}
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["keys"]) == 1
        new_key = data["keys"][0]
        assert new_key["key"].startswith("dip_")
        assert new_key["key"] != raw_key
        assert new_key["plan"] == "demo"
        assert new_key["monthly_page_quota"] == 20

    def test_recover_deactivates_old_keys(self) -> None:
        client, database = _app()
        raw_key = client.post(
            "/v1/demo-keys", json={"email": "recover@example.com"}
        ).json()["api_key"]
        client.post("/v1/keys/recover", json={"email": "recover@example.com"})
        # Old key should be deactivated
        with database.session_factory() as s:
            old = (
                s.query(ApiKey)
                .filter(ApiKey.key_hash == hash_api_key(raw_key))
                .one()
            )
            assert old.active is False

    def test_recover_preserves_paddle_customer_id(self) -> None:
        client, database = _app()
        raw_key = client.post(
            "/v1/demo-keys", json={"email": "sub@example.com"}
        ).json()["api_key"]
        # Simulate subscription by setting customer_id
        with database.session_factory() as s:
            key = (
                s.query(ApiKey)
                .filter(ApiKey.key_hash == hash_api_key(raw_key))
                .one()
            )
            key.paddle_customer_id = "ctm_01test123"
            key.plan = "pro"
            key.monthly_page_quota = 5000
            s.commit()
        response = client.post(
            "/v1/keys/recover", json={"email": "sub@example.com"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["keys"][0]["plan"] == "pro"
        assert data["keys"][0]["monthly_page_quota"] == 5000

    def test_recover_unknown_email_returns_404(self) -> None:
        client, _db = _app()
        response = client.post(
            "/v1/keys/recover", json={"email": "unknown@example.com"}
        )
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "email_not_found"

    def test_recover_no_email_returns_422(self) -> None:
        client, _db = _app()
        response = client.post("/v1/keys/recover", json={})
        assert response.status_code == 422

    def test_recover_invalid_email_returns_422(self) -> None:
        client, _db = _app()
        response = client.post("/v1/keys/recover", json={"email": "bad"})
        assert response.status_code == 422

    def test_recovered_key_is_usable(self) -> None:
        client, database = _app()
        client.post("/v1/demo-keys", json={"email": "usable@example.com"})
        response = client.post(
            "/v1/keys/recover", json={"email": "usable@example.com"}
        )
        new_key = response.json()["keys"][0]["key"]
        # Use the new key to parse a document
        parse_response = client.post(
            "/v1/parse",
            headers={"X-API-Key": new_key},
            files={"file": ("sample.txt", b"hello", "text/plain")},
        )
        assert parse_response.status_code == 200


class TestLandingPageEmailRecovery:
    """Landing page is auth-gated; key recovery stays available via API."""

    def test_landing_has_no_email_input(self) -> None:
        client, _db = _app()
        response = client.get("/")
        assert response.status_code == 200
        assert 'id="email-input"' not in response.text

    def test_landing_links_to_login(self) -> None:
        client, _db = _app()
        response = client.get("/")
        assert 'href="/login"' in response.text

    def test_recovery_api_still_available(self) -> None:
        client, _db = _app()
        response = client.post("/v1/keys/recover", json={"email": "x@example.com"})
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "email_not_found"

    def test_landing_checkout_sends_email(self) -> None:
        client, _db = _app()
        response = client.get("/")
        assert "customer" in response.text and "email" in response.text

    def test_success_page_mentions_recovery(self) -> None:
        client, _db = _app()
        response = client.get("/billing/success")
        assert response.status_code == 200
        assert "recover" in response.text.lower()


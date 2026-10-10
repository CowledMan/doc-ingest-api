"""Optional Postgres compatibility tests.

These tests only run when ``TEST_DATABASE_URL`` is set to a Postgres
connection string. In CI without Postgres they are skipped.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import create_engine

from doc_ingest_api.models import ApiKey, Base
from doc_ingest_api.security import create_api_key, find_api_key

pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="TEST_DATABASE_URL not set; Postgres tests skipped",
    ),
    pytest.mark.integration,
]


@pytest.fixture
def pg_database() -> Any:
    """Create an ephemeral Postgres database session factory."""
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_postgres_create_and_find_api_key(pg_database: Any) -> None:
    """Verify ApiKey CRUD works on Postgres."""
    raw_key = create_api_key(pg_database, name="pg-test")
    with pg_database() as session:
        key = find_api_key(session, raw_key)
        assert key is not None
        assert key.name == "pg-test"
        assert key.plan == "free"
        assert key.monthly_page_quota == 500


def test_postgres_plan_and_expiry_columns(pg_database: Any) -> None:
    """Verify plan and expires_at columns work on Postgres."""
    from datetime import UTC, datetime, timedelta

    raw_key = create_api_key(pg_database, name="pg-demo", monthly_page_quota=20)
    with pg_database() as session:
        key = find_api_key(session, raw_key)
        assert key is not None
        key.plan = "demo"
        key.expires_at = datetime.now(UTC) + timedelta(hours=24)
        session.commit()

    with pg_database() as session:
        key = session.query(ApiKey).filter_by(name="pg-demo").one()
        assert key.plan == "demo"
        assert key.expires_at is not None


def test_resolve_driver_rewrites_bare_postgres_url() -> None:
    from doc_ingest_api.database import _resolve_driver

    url = "postgresql://user:pass@host/db?sslmode=require"
    assert _resolve_driver(url) == "postgresql+psycopg://user:pass@host/db?sslmode=require"


def test_resolve_driver_keeps_explicit_dialect() -> None:
    from doc_ingest_api.database import _resolve_driver

    assert _resolve_driver("postgresql+psycopg://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert _resolve_driver("sqlite:///./app.db") == "sqlite:///./app.db"

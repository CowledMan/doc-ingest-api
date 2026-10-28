"""Tests for the free-key quota backfill migration."""

from __future__ import annotations

import time
from typing import Any

from doc_ingest_api.database import create_database
from doc_ingest_api.models import ApiKey


def _add_key(database: Any, name: str, key_hash: str, **kwargs: Any) -> None:
    with database.session_factory.begin() as session:
        session.add(ApiKey(name=name, key_hash=key_hash, **kwargs))


def test_backfill_keeps_only_newest_free_key() -> None:
    database = create_database("sqlite://")
    database.create_all()
    _add_key(database, "old", "a" * 64, monthly_page_quota=5, plan="free", owner_user_id=1)
    time.sleep(0.02)
    _add_key(database, "new", "b" * 64, monthly_page_quota=5, plan="free", owner_user_id=1)
    database._backfill_free_key_quotas()
    with database.session_factory() as session:
        quotas = {k.name: k.monthly_page_quota for k in session.query(ApiKey).all()}
    assert quotas == {"old": 0, "new": 5}


def test_backfill_is_idempotent() -> None:
    database = create_database("sqlite://")
    database.create_all()
    _add_key(database, "solo", "c" * 64, monthly_page_quota=5, plan="free", owner_user_id=7)
    database._backfill_free_key_quotas()
    database._backfill_free_key_quotas()
    with database.session_factory() as session:
        key = session.query(ApiKey).one()
    assert key.monthly_page_quota == 5


def test_backfill_ignores_revoked_and_demo_keys() -> None:
    database = create_database("sqlite://")
    database.create_all()
    _add_key(database, "revoked", "d" * 64, monthly_page_quota=5, plan="free", owner_user_id=2, active=False)
    _add_key(database, "demo", "e" * 64, monthly_page_quota=20, plan="demo", owner_user_id=2)
    _add_key(database, "extra", "f" * 64, monthly_page_quota=5, plan="free", owner_user_id=2)
    database._backfill_free_key_quotas()
    with database.session_factory() as session:
        quotas = {k.name: k.monthly_page_quota for k in session.query(ApiKey).all()}
    assert quotas == {"revoked": 5, "demo": 20, "extra": 5}


def test_backfill_handles_separate_users() -> None:
    database = create_database("sqlite://")
    database.create_all()
    _add_key(database, "u1", "1" * 64, monthly_page_quota=5, plan="free", owner_user_id=1)
    _add_key(database, "u2", "2" * 64, monthly_page_quota=5, plan="free", owner_user_id=2)
    database._backfill_free_key_quotas()
    with database.session_factory() as session:
        quotas = {k.name: k.monthly_page_quota for k in session.query(ApiKey).all()}
    assert quotas == {"u1": 5, "u2": 5}

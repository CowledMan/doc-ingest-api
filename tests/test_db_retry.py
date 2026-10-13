"""Tests for database cold-start retry in _get_db."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from doc_ingest_api.app import create_app
from doc_ingest_api.config import Settings


class FlakySessionFactory:
    """Session factory that raises OperationalError on the first N calls."""

    def __init__(self, fail_count: int = 1) -> None:
        self._fail_count = fail_count
        self._calls = 0

    def __call__(self) -> Any:
        self._calls += 1
        if self._calls <= self._fail_count:
            raise OperationalError(
                "connection failed", {"e": "cold start"}, Exception("connection refused")
            )
        # Return a mock session that has close()
        return _MockSession()


class _MockSession:
    def close(self) -> None:
        pass


def _test_settings(tmp_path: Path) -> Settings:
    """Build minimal test settings."""
    return Settings(
        database_url=f"sqlite:///{tmp_path}/test.db",
        redis_url="redis://localhost:6379/0",
        queue_backend="background",
    )


def test_db_retry_succeeds_on_second_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First connection fails with OperationalError, retry succeeds."""
    settings = _test_settings(tmp_path)

    flaky = FlakySessionFactory(fail_count=1)

    # Monkeypatch create_database to return our flaky factory
    class FakeDatabase:
        engine: Any = None
        session_factory: Any = flaky

        def create_all(self) -> None:
            pass

    monkeypatch.setattr(
        "doc_ingest_api.database.create_database",
        lambda url: FakeDatabase(),
    )

    app = create_app(settings)
    # Replace the session factory with our flaky one

    # Patch the app state's session_factory
    # Actually, create_app uses _app_state(request).session_factory
    # Let's just monkeypatch the session_factory dependency
    app.dependency_overrides.clear()

    # We need to override the session_factory dependency
    from doc_ingest_api.app import _session_factory

    app.dependency_overrides[_session_factory] = lambda: flaky

    # Override _get_api_key to skip auth
    # We need a valid api key in the DB but our session factory is mocked
    # So let's just test _get_db directly

    # The healthz endpoint doesn't use _get_db, so use a simpler approach:
    # monkeypatch time.sleep to be instant
    monkeypatch.setattr("doc_ingest_api.app.time.sleep", lambda s: None)

    # Test _get_db function directly

    from doc_ingest_api.app import _get_db

    # Call _get_db as a generator
    gen = _get_db(flaky)
    session = next(gen)
    assert session is not None
    gen.close()
    assert flaky._calls == 2  # First call failed, second succeeded


def test_db_persistent_failure_returns_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When the database is permanently unavailable, _get_db raises 503."""
    always_fail = FlakySessionFactory(fail_count=999)

    monkeypatch.setattr("doc_ingest_api.app.time.sleep", lambda s: None)

    from fastapi import HTTPException

    from doc_ingest_api.app import _get_db

    with pytest.raises(HTTPException) as exc_info:
        # Consume the generator - the exception should be raised
        gen = _get_db(always_fail)
        next(gen)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "service_unavailable"
    assert always_fail._calls == 2  # max_retries


def test_db_retry_no_delay_on_first_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal operation: no sleep, session created on first attempt."""
    good = FlakySessionFactory(fail_count=0)

    sleeps: list[float] = []
    monkeypatch.setattr("doc_ingest_api.app.time.sleep", lambda s: sleeps.append(s))

    from doc_ingest_api.app import _get_db

    gen = _get_db(good)
    session = next(gen)
    assert session is not None
    gen.close()
    assert good._calls == 1
    assert len(sleeps) == 0

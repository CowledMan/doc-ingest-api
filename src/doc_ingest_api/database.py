"""Database engine and session factory helpers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import ApiKey, Base


@dataclass(frozen=True)
class Database:
    """A configured SQLAlchemy engine and session factory."""

    engine: Engine
    session_factory: sessionmaker[Session]

    def create_all(self) -> None:
        """Create all model tables and run lightweight migrations."""
        Base.metadata.create_all(self.engine)
        self._migrate_add_columns()
        self._backfill_free_key_quotas()

    def _migrate_add_columns(self) -> None:
        """Add columns that are missing from pre-existing tables.

        ``create_all`` only creates new tables; it does not alter existing
        ones. This method detects missing columns and issues idempotent
        ``ALTER TABLE ... ADD COLUMN`` statements compatible with SQLite
        and Postgres.
        """
        inspector = inspect(self.engine)
        if not inspector.has_table("api_keys"):
            return
        existing = {col["name"] for col in inspector.get_columns("api_keys")}
        migrations = [
            ("paddle_customer_id", "VARCHAR(64)"),
            ("owner_email", "VARCHAR(255)"),
            ("key_prefix", "VARCHAR(16)"),
            ("owner_user_id", "INTEGER"),
        ]
        with self.engine.begin() as conn:
            for column_name, column_type in migrations:
                if column_name not in existing:
                    conn.execute(
                        text(f"ALTER TABLE api_keys ADD COLUMN {column_name} {column_type}")
                    )
        if inspector.has_table("parse_jobs"):
            job_columns = {col["name"] for col in inspector.get_columns("parse_jobs")}
            if "pages_used" not in job_columns:
                with self.engine.begin() as conn:
                    conn.execute(text("ALTER TABLE parse_jobs ADD COLUMN pages_used INTEGER"))
                    conn.execute(
                        text("UPDATE parse_jobs SET pages_used = 0 WHERE pages_used IS NULL")
                    )
                    conn.execute(
                        text("UPDATE parse_jobs SET pages_used = 0 WHERE pages_used IS NULL")
                    )

    def _backfill_free_key_quotas(self) -> None:
        """Keep the free allowance on only the oldest active free key per user.

        Keys created before the one-free-allowance rule shipped may all carry
        the free quota; this idempotent backfill zeroes the extra ones so a
        user never holds more than one active 5-page free key.
        """
        try:
            with self.session_factory.begin() as session:
                rows = (
                    session.execute(
                        select(ApiKey)
                        .where(
                            ApiKey.active == True,  # noqa: E712
                            ApiKey.plan == "free",
                            ApiKey.owner_user_id.is_not(None),
                        )
                        .order_by(ApiKey.owner_user_id, ApiKey.created_at)
                    )
                    .scalars()
                    .all()
                )
                seen_users: set[int] = set()
                for key in rows:
                    if key.owner_user_id is None:
                        continue
                    if key.owner_user_id not in seen_users:
                        seen_users.add(key.owner_user_id)
                        continue
                    if key.monthly_page_quota != 0:
                        key.monthly_page_quota = 0
        except Exception:
            return


def _resolve_driver(database_url: str) -> str:
    """Default bare ``postgresql://`` URLs to the installed psycopg3 driver.

    Operators pass standard ``postgresql://`` connection strings (e.g. from
    Neon or Render), but SQLAlchemy needs an explicit dialect+driver, so
    rewrite to ``postgresql+psycopg://`` unless one is already specified.
    """
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def create_database(database_url: str) -> Database:
    """Create a database engine and session factory for the given URL."""
    database_url = _resolve_driver(database_url)
    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    elif database_url.startswith("postgresql+psycopg"):
        # Disable psycopg3 server-side prepared statements for PgBouncer
        # transaction-mode compatibility (e.g. Neon pooler endpoints).
        connect_args["prepare_threshold"] = None
    poolclass = StaticPool if database_url in {"sqlite://", "sqlite:///:memory:"} else None
    engine = create_engine(database_url, connect_args=connect_args, poolclass=poolclass)
    return Database(engine=engine, session_factory=sessionmaker(bind=engine, expire_on_commit=False))

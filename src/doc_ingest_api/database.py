"""Database engine and session factory helpers."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base


@dataclass(frozen=True)
class Database:
    """A configured SQLAlchemy engine and session factory."""

    engine: Engine
    session_factory: sessionmaker[Session]

    def create_all(self) -> None:
        """Create all model tables."""
        Base.metadata.create_all(self.engine)


def create_database(database_url: str) -> Database:
    """Create a database engine and session factory for the given URL."""
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    poolclass = StaticPool if database_url in {"sqlite://", "sqlite:///:memory:"} else None
    engine = create_engine(database_url, connect_args=connect_args, poolclass=poolclass)
    return Database(engine=engine, session_factory=sessionmaker(bind=engine, expire_on_commit=False))

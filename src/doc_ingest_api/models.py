"""SQLAlchemy models for API keys and asynchronous parse jobs."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def current_period_start() -> date:
    """Return the first day of the current UTC month."""
    return datetime.now(UTC).date().replace(day=1)


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Shared SQLAlchemy declarative base."""


class ApiKey(Base):
    """A hashed API key with a rolling monthly page quota."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    monthly_page_quota: Mapped[int] = mapped_column(Integer, default=500)
    used_pages: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(default=True)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    paddle_customer_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    owner_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True, default=None
    )
    key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, default=None, index=True
    )
    period_start: Mapped[date] = mapped_column(Date, default=current_period_start)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    """A registered customer account."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    email_verified: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ParseJob(Base):
    """One queued or completed asynchronous parse request."""

    __tablename__ = "parse_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    file_name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine_used: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

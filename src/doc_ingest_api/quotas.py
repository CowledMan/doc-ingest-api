"""Monthly page quota accounting."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from .models import ApiKey, current_period_start


def ensure_monthly_period(api_key: ApiKey) -> bool:
    """Reset usage in place when the stored period is not the current month."""
    period = current_period_start()
    if api_key.period_start == period:
        return False
    api_key.period_start = period
    api_key.used_pages = 0
    return True


def record_page_usage(
    session_factory: sessionmaker[Session],
    *,
    api_key_id: int,
    pages: int,
) -> None:
    """Add parsed pages to the key's current monthly period."""
    if pages < 0:
        raise ValueError("pages must be non-negative")
    with session_factory.begin() as session:
        api_key = session.get(ApiKey, api_key_id)
        if api_key is None:
            return
        ensure_monthly_period(api_key)
        api_key.used_pages += pages

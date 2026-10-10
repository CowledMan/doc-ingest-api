"""Document ingestion API product package."""

from __future__ import annotations

from .app import create_app
from .config import Settings

__version__ = "0.1.0"

__all__ = ["Settings", "create_app"]

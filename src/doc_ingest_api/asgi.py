"""ASGI entry point for uvicorn."""

from __future__ import annotations

from .app import create_app
from .config import Settings

app = create_app(Settings())

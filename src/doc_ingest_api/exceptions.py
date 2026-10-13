"""Shared exception types and data classes for parser routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParseResult:
    """A single parser result and its routing metadata."""

    markdown: str
    confidence: float
    metadata: dict[str, Any]
    engine_used: str


class ParserError(RuntimeError):
    """Raised when a parser cannot produce a usable Markdown result."""


class ParserTimeoutError(ParserError):
    """Raised when parser execution exceeds the configured timeout."""


class OcrNotConfiguredError(ParserError):
    """Raised when a scanned document reaches OCR but no OCR key is present."""

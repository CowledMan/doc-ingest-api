"""Parser protocols, subprocess adapters, and confidence-based routing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .config import Settings


class ParserError(RuntimeError):
    """Raised when a parser cannot produce a usable Markdown result."""


class ParserTimeoutError(ParserError):
    """Raised when parser execution exceeds the configured timeout."""


class OcrNotConfiguredError(ParserError):
    """Raised when a scanned document reaches OCR but no OCR key is present."""


@dataclass(frozen=True)
class ParseResult:
    """A single parser result and its routing metadata."""

    markdown: str
    confidence: float
    metadata: dict[str, Any]
    engine_used: str


@runtime_checkable
class ParserProtocol(Protocol):
    """Contract implemented by every parsing engine adapter."""

    def parse(self, file: Path) -> ParseResult:
        """Parse one document and return Markdown with routing metadata."""
        ...


@dataclass(frozen=True)
class SubprocessParser:
    """Run a parser in an isolated subprocess with a hard timeout."""

    engine: str
    python_executable: Path
    timeout_seconds: float

    def parse(self, file: Path) -> ParseResult:
        """Parse ``file`` using the configured engine subprocess."""
        command = [
            str(self.python_executable),
            "-m",
            "doc_ingest_api.engine_worker",
            self.engine,
            str(file),
        ]
        try:
            process = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=_subprocess_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ParserTimeoutError(
                f"{self.engine} timed out after {self.timeout_seconds:.0f} seconds"
            ) from exc
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or "unknown parser error"
            raise ParserError(f"{self.engine} failed: {detail}")

        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise ParserError(f"{self.engine} returned invalid JSON") from exc
        markdown = str(payload.get("markdown", ""))
        metadata_raw = payload.get("metadata", {})
        metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
        return ParseResult(
            markdown=markdown,
            confidence=float(payload.get("confidence", 0.0)),
            metadata=metadata,
            engine_used=self.engine,
        )


@dataclass(frozen=True)
class AliyunOcrStubParser:
    """Stub for the pay-as-you-go Aliyun OCR route."""

    api_key: str = ""

    def parse(self, file: Path) -> ParseResult:
        """Reject unconfigured OCR and keep provider implementation out of M1 batch 1."""
        if not self.api_key:
            raise OcrNotConfiguredError(
                "OCR is required for this document, but ALIYUN_OCR_KEY is not configured"
            )
        raise ParserError("Aliyun OCR adapter is not implemented in this batch")


@dataclass(frozen=True)
class ParserRouter:
    """Route documents by category, then by confidence threshold."""

    parsers: Mapping[str, ParserProtocol]
    confidence_threshold: float = 0.6

    def parse(self, file: Path, category: str | None = None) -> ParseResult:
        """Parse a document through the category's ordered parser chain."""
        attempts: list[ParseResult] = []
        for engine in self._route(category):
            parser = self.parsers.get(engine)
            if parser is None:
                continue
            try:
                result = parser.parse(file)
            except OcrNotConfiguredError:
                raise
            except ParserTimeoutError:
                raise
            except ParserError:
                continue
            if result.confidence >= self.confidence_threshold:
                return result
            attempts.append(result)
        if attempts:
            return max(attempts, key=lambda item: item.confidence)
        raise ParserError("no configured parser produced a result")

    def _route(self, category: str | None) -> tuple[str, ...]:
        """Return the parser chain for one document category."""
        if category == "financial_reports":
            return ("anydoc", "markitdown", "docling")
        if category in {"scanned_documents", "official_red_header"}:
            return ("ocr", "docling", "markitdown", "anydoc")
        return ("markitdown", "anydoc", "docling")


def create_parser_router(settings: Settings) -> ParserRouter:
    """Create the production subprocess parser router from settings."""
    default_python = Path(sys.executable)
    return ParserRouter(
        parsers={
            "anydoc": SubprocessParser(
                engine="anydoc",
                python_executable=settings.parser_anydoc_python or default_python,
                timeout_seconds=settings.parser_timeout_seconds,
            ),
            "markitdown": SubprocessParser(
                engine="markitdown",
                python_executable=settings.parser_markitdown_python or default_python,
                timeout_seconds=settings.parser_timeout_seconds,
            ),
            "docling": SubprocessParser(
                engine="docling",
                python_executable=settings.parser_docling_python or default_python,
                timeout_seconds=settings.parser_timeout_seconds,
            ),
            "ocr": AliyunOcrStubParser(api_key=settings.aliyun_ocr_key),
        },
        confidence_threshold=settings.confidence_threshold,
    )


def _subprocess_environment() -> dict[str, str]:
    """Create a child environment that can import this package from source."""
    environment = os.environ.copy()
    source_root = Path(__file__).resolve().parents[2] / "src"
    if source_root.is_dir():
        current = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            f"{source_root}{os.pathsep}{current}" if current else str(source_root)
        )
    return environment

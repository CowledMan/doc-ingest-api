"""Integration tests against the real parsing engines."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from doc_ingest_api.config import Settings
from doc_ingest_api.parsers import ParserRouter, SubprocessParser

pytestmark = pytest.mark.integration

SAMPLE_PDF = Path("benchmarks/dataset/inputs/contracts/contracts_001.pdf")


@pytest.fixture
def sample_pdf() -> Path:
    """Return a real benchmark PDF, skipping when local samples are absent."""
    if not SAMPLE_PDF.is_file():
        pytest.skip("real benchmark sample is not available")
    return SAMPLE_PDF


def test_router_parses_real_contract_with_markitdown(sample_pdf: Path) -> None:
    """Guard against losing production parser dependencies from the main venv."""
    pytest.importorskip("markitdown")
    settings = Settings()
    router = ParserRouter(
        parsers={
            "markitdown": SubprocessParser(
                engine="markitdown",
                python_executable=Path(sys.executable),
                timeout_seconds=settings.parser_timeout_seconds,
            )
        },
        confidence_threshold=settings.confidence_threshold,
    )

    result = router.parse(sample_pdf)

    assert result.engine_used == "markitdown"
    assert result.markdown.strip()

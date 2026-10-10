"""Tests for the M0 comparison report renderer."""

from __future__ import annotations

from benchmarks.engine_comparison import (
    ComparisonRecord,
    aggregate_records,
    build_markdown_report,
)
from benchmarks.structure_metrics import StructureMetrics


def _record(engine: str = "anydoc") -> ComparisonRecord:
    """Return one representative comparison record."""
    return ComparisonRecord(
        engine=engine,
        category="contracts",
        doc_id="contracts_001",
        status="ok",
        failure_reason="",
        elapsed_seconds=1.0,
        output_chars=10,
        metrics=StructureMetrics(1, 1, 1, 1, 0, 0),
        agreement_avg=0.5,
    )


def test_report_marks_partial_engine_in_tables_and_coverage() -> None:
    record = _record()
    aggregation = aggregate_records([record], ["contracts"])

    report = build_markdown_report(
        aggregation,
        ("anydoc",),
        ["contracts"],
        120.0,
        partial_engines=("anydoc",),
    )

    assert "`anydoc`（partial）" in report
    assert "状态 partial（中断前保留结果，未补跑）" in report
    assert record.doc_id not in report

"""Tests for benchmark metric scaffolding."""

from __future__ import annotations

from benchmarks.metrics import score_document


def test_score_document_matches_perfect_tables_formulas_and_layout() -> None:
    ground_truth = """# 标题\n\n正文。\n\n| 名称 | 数量 |\n| --- | ---: |\n| 项目 | 3 |\n\n$$E=mc^2$$\n"""
    scores = score_document(ground_truth, ground_truth)
    assert scores.table_restoration == 1.0
    assert scores.formula_preservation == 1.0
    assert scores.layout_fidelity == 1.0
    assert scores.overall == 1.0


def test_score_document_detects_missing_table_and_formula() -> None:
    ground_truth = """# 标题\n\n| 名称 | 数量 |\n| --- | ---: |\n| 项目 | 3 |\n\n$$E=mc^2$$\n"""
    output = "# 标题\n\n正文丢失。\n"
    scores = score_document(ground_truth, output)
    assert scores.table_restoration < 1.0
    assert scores.formula_preservation < 1.0
    assert scores.layout_fidelity < 1.0
    assert scores.overall < 1.0

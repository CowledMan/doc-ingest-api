"""Deterministic baseline metrics for the M0 benchmark framework.

These metrics are intentionally simple and reproducible. Human review remains
required for complex layout, handwriting, seals, and red-header documents.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_SEPARATOR_LINE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_FORMULA = re.compile(r"\$\$[^$]+\$\$|\$[^$\n]+\$")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class DocumentScores:
    """Normalized metric values in the inclusive range 0..1."""

    table_restoration: float
    formula_preservation: float
    layout_fidelity: float
    overall: float
    details: dict[str, dict[str, float]]

    def to_dict(self) -> dict[str, float | dict[str, dict[str, float]]]:
        return asdict(self)


def _normalize(value: str) -> str:
    return _WHITESPACE.sub(" ", value.strip().casefold())


def _parse_table_row(line: str) -> list[str]:
    return [_normalize(cell) for cell in line.strip().strip("|").split("|")]


def _extract_tables(markdown: str) -> list[list[list[str]]]:
    """Extract contiguous Markdown pipe tables as rows of normalized cells."""
    tables: list[list[list[str]]] = []
    block: list[list[str]] = []

    def flush() -> None:
        if block:
            tables.append(block.copy())
            block.clear()

    for line in markdown.splitlines():
        if not _TABLE_LINE.match(line):
            flush()
            continue
        if _SEPARATOR_LINE.match(line):
            continue
        block.append(_parse_table_row(line))
    flush()
    return tables


def _extract_formulas(markdown: str) -> set[str]:
    return {_normalize(match.group(0)) for match in _FORMULA.finditer(markdown)}


def _extract_layout_lines(markdown: str) -> list[str]:
    return [_normalize(line) for line in markdown.splitlines() if _normalize(line)]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def table_restoration_score(ground_truth: str, output: str) -> tuple[float, dict[str, float]]:
    """Score table rows and cells by overlap with the ground truth."""
    gt_tables = _extract_tables(ground_truth)
    out_tables = _extract_tables(output)
    gt_rows = {tuple(row) for table in gt_tables for row in table if any(row)}
    out_rows = {tuple(row) for table in out_tables for row in table if any(row)}
    if not gt_rows:
        return 1.0, {"gt_table_count": float(len(gt_tables)), "out_table_count": float(len(out_tables))}
    gt_cells = {cell for row in gt_rows for cell in row}
    out_cells = {cell for row in out_rows for cell in row}
    row_recall = len(gt_rows & out_rows) / len(gt_rows) if gt_rows else 0.0
    cell_recall = len(gt_cells & out_cells) / len(gt_cells) if gt_cells else 0.0
    score = _clamp01(0.70 * row_recall + 0.30 * cell_recall)
    return score, {
        "row_recall": row_recall,
        "cell_recall": cell_recall,
        "gt_table_count": float(len(gt_tables)),
        "out_table_count": float(len(out_tables)),
    }


def formula_preservation_score(ground_truth: str, output: str) -> tuple[float, dict[str, float]]:
    """Score LaTeX-like formula preservation after normalization."""
    gt_formulas = _extract_formulas(ground_truth)
    out_formulas = _extract_formulas(output)
    if not gt_formulas:
        return 1.0, {"gt_formula_count": float(len(gt_formulas)), "out_formula_count": float(len(out_formulas))}
    recall = len(gt_formulas & out_formulas) / len(gt_formulas)
    precision = len(gt_formulas & out_formulas) / len(out_formulas) if out_formulas else 0.0
    return _clamp01(0.75 * recall + 0.25 * precision), {
        "recall": recall,
        "precision": precision,
        "gt_formula_count": float(len(gt_formulas)),
        "out_formula_count": float(len(out_formulas)),
    }


def layout_fidelity_score(ground_truth: str, output: str) -> tuple[float, dict[str, float]]:
    """Score reading order using headings and non-empty line sequence."""
    gt_lines = _extract_layout_lines(ground_truth)
    out_lines = _extract_layout_lines(output)
    if not gt_lines:
        return 0.0, {"gt_line_count": 0.0, "out_line_count": float(len(out_lines))}
    ratio = SequenceMatcher(None, gt_lines, out_lines, autojunk=False).ratio()
    return _clamp01(ratio), {
        "sequence_ratio": ratio,
        "gt_line_count": float(len(gt_lines)),
        "out_line_count": float(len(out_lines)),
    }


def score_document(ground_truth: str, output: str) -> DocumentScores:
    """Compute all benchmark scores for one document.

    Overall weighting is fixed for M0 comparability:
    tables 45%, formulas 20%, layout 35%.
    """
    table_score, table_details = table_restoration_score(ground_truth, output)
    formula_score, formula_details = formula_preservation_score(ground_truth, output)
    layout_score, layout_details = layout_fidelity_score(ground_truth, output)
    overall = 0.45 * table_score + 0.20 * formula_score + 0.35 * layout_score
    return DocumentScores(
        table_restoration=table_score,
        formula_preservation=formula_score,
        layout_fidelity=layout_score,
        overall=overall,
        details={
            "table": table_details,
            "formula": formula_details,
            "layout": layout_details,
        },
    )



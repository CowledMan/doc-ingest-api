"""Lightweight Markdown confidence estimation."""

from __future__ import annotations

import re

_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_TABLE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]\s+\S|\d+[.)]\s+\S)", re.MULTILINE)


def estimate_confidence(markdown: str) -> float:
    """Estimate structural confidence for a parser Markdown result.

    This is intentionally conservative: M0 has no ground truth, so it detects
    obvious empty/low-structure outputs and leaves detailed confidence scoring
    to M1's ground-truth-calibrated evaluator.
    """
    text = markdown.strip()
    if not text:
        return 0.0

    confidence = 0.55
    if len(text) >= 120:
        confidence += 0.15
    if _HEADING.search(text):
        confidence += 0.10
    if _TABLE.search(text):
        confidence += 0.10
    if _LIST_ITEM.search(text):
        confidence += 0.05
    return min(0.95, confidence)

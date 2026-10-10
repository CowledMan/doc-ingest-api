"""Structural metrics extracted from Markdown engine outputs.

These metrics are computed without ground truth: they describe what each
engine produced so outputs can be compared for structure richness and for
agreement between engines.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_HEADING = re.compile(r"^(#{1,6})\s+\S", re.MULTILINE)
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_LIST_ITEM = re.compile(r"^\s{0,3}(?:[-*+]\s+\S|\d+[.)]\s+\S)", re.MULTILINE)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_BLOCK_FORMULA = re.compile(r"\$\$[^$]+\$\$", re.DOTALL)
_INLINE_FORMULA = re.compile(r"(?<!\$)\$(?!\$)[^$\n]+\$(?!\$)")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class StructureMetrics:
    """Counts of structural elements in one Markdown document."""

    table_count: int
    heading_count: int
    heading_levels: int
    list_item_count: int
    image_count: int
    formula_count: int

    def to_dict(self) -> dict[str, int]:
        """Return the metrics as a plain dict."""
        return asdict(self)


def extract_structure_metrics(markdown: str) -> StructureMetrics:
    """Count tables, headings, list items, images, and formula markers."""
    heading_matches = list(_HEADING.finditer(markdown))
    heading_levels = len({match.group(1).count("#") for match in heading_matches})

    table_count = 0
    block_size = 0
    for line in markdown.splitlines():
        if _TABLE_LINE.match(line):
            block_size += 1
        else:
            if block_size >= 2:
                table_count += 1
            block_size = 0
    if block_size >= 2:
        table_count += 1

    formula_count = len(_BLOCK_FORMULA.findall(markdown)) + len(_INLINE_FORMULA.findall(markdown))
    return StructureMetrics(
        table_count=table_count,
        heading_count=len(heading_matches),
        heading_levels=heading_levels,
        list_item_count=len(_LIST_ITEM.findall(markdown)),
        image_count=len(_IMAGE.findall(markdown)),
        formula_count=formula_count,
    )


def normalize_text(text: str) -> str:
    """Normalize text for agreement comparison: lowercase, no whitespace."""
    return _WHITESPACE.sub("", text).casefold()


def _char_ngrams(text: str) -> set[str]:
    """Return character bigrams (or unigrams for very short text)."""
    if len(text) < 2:
        return set(text)
    return {text[i : i + 2] for i in range(len(text) - 1)}


def jaccard_similarity(left: str, right: str) -> float:
    """Jaccard similarity of normalized texts via character bigrams."""
    left_grams = _char_ngrams(normalize_text(left))
    right_grams = _char_ngrams(normalize_text(right))
    if not left_grams and not right_grams:
        return 1.0
    union = left_grams | right_grams
    if not union:
        return 1.0
    return len(left_grams & right_grams) / len(union)
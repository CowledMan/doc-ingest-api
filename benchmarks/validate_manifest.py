"""Validate the real-document benchmark manifest.

Checks that every manifest row has the required metadata fields, that the
input file exists, that the recorded SHA256 matches the file, and that every
category meets the minimum count quota (80 percent of 25 by default).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .scaffold_dataset import CATEGORIES

_SPEC_BY_KEY = {spec.key: spec for spec in CATEGORIES}

REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "category",
    "category_label",
    "input",
    "ground_truth",
    "features",
    "language",
    "status",
    "title",
    "source_url",
    "sha256",
    "downloaded_at",
)


def load_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    """Load manifest rows, skipping blank lines."""
    rows: list[dict[str, Any]] = []
    with manifest_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"manifest row at line {line_number} is not a JSON object")
            rows.append(row)
    if not rows:
        raise ValueError(f"manifest has no rows: {manifest_path}")
    return rows


def validate_manifest(
    manifest_path: Path, *, min_count: int = 20
) -> dict[str, Any]:
    """Validate files, hashes, and per-category counts.

    Returns a report dict; raises nothing. Callers decide pass/fail from
    ``report["ok"]``.
    """
    errors: list[str] = []
    rows = load_manifest(manifest_path)
    dataset_dir = manifest_path.parent

    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    per_category: dict[str, int] = {}

    for line_number, row in enumerate(rows, 1):
        row_id = str(row.get("id", ""))
        category = str(row.get("category", ""))
        label = f"line {line_number} ({row_id or 'no-id'})"
        for field in REQUIRED_FIELDS:
            if field not in row or row[field] in (None, ""):
                errors.append(f"{label}: missing field {field}")
        if category not in _SPEC_BY_KEY:
            errors.append(f"{label}: unknown category {category!r}")
            continue
        if row_id in seen_ids:
            errors.append(f"{label}: duplicate id {row_id!r}")
        seen_ids.add(row_id)
        per_category[category] = per_category.get(category, 0) + 1

        digest = str(row.get("sha256", ""))
        if digest:
            if digest in seen_hashes:
                errors.append(f"{label}: duplicate sha256")
            seen_hashes.add(digest)

        input_relative = str(row.get("input", ""))
        input_path = dataset_dir / input_relative
        if not input_path.is_file():
            errors.append(f"{label}: missing input file {input_relative}")
            continue
        actual = hashlib.sha256(input_path.read_bytes()).hexdigest()
        if actual != digest:
            errors.append(f"{label}: sha256 mismatch (manifest {digest} != actual {actual})")

    for category in _SPEC_BY_KEY:
        count = per_category.get(category, 0)
        if count < min_count:
            errors.append(
                f"category {category}: {count} document(s) below minimum {min_count}"
            )

    report: dict[str, Any] = {
        "manifest": str(manifest_path),
        "total_rows": len(rows),
        "per_category": {
            spec.key: {"count": per_category.get(spec.key, 0), "label": spec.label}
            for spec in CATEGORIES
        },
        "min_count": min_count,
        "errors": errors,
        "ok": not errors,
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/dataset/00_manifest.jsonl"),
    )
    parser.add_argument("--min-count", type=int, default=20)
    return parser


def main() -> int:
    """CLI entry point; returns a process exit code."""
    args = build_parser().parse_args()
    if args.min_count < 1:
        raise ValueError("--min-count must be >= 1")
    report = validate_manifest(args.manifest, min_count=args.min_count)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
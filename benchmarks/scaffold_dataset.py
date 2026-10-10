"""Create the 200-document Chinese benchmark manifest and directory skeleton."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CategorySpec:
    """Specification for one benchmark category."""

    key: str
    label: str
    extension: str
    features: tuple[str, ...]


CATEGORIES: tuple[CategorySpec, ...] = (
    CategorySpec("contracts", "中文合同", ".pdf", ("tables", "chinese_text", "layout")),
    CategorySpec("academic_papers", "中文学术论文", ".pdf", ("layout", "formulas", "tables")),
    CategorySpec("financial_reports", "中文财报", ".pdf", ("tables", "layout", "chinese_text")),
    CategorySpec("scanned_documents", "中文扫描件", ".pdf", ("ocr", "tables", "layout")),
    CategorySpec("presentations", "中文PPT", ".pptx", ("layout", "tables", "chinese_text")),
    CategorySpec("spreadsheets", "中文Excel", ".xlsx", ("tables", "chinese_text")),
    CategorySpec("official_red_header", "红头公文", ".pdf", ("layout", "chinese_text")),
    CategorySpec("formula_heavy", "公式密集文档", ".pdf", ("formulas", "layout", "chinese_text")),
)


def iter_manifest_entries(count_per_category: int) -> list[dict[str, object]]:
    """Return 200 placeholder manifest rows by default."""
    rows: list[dict[str, object]] = []
    for spec in CATEGORIES:
        for index in range(1, count_per_category + 1):
            doc_id = f"{spec.key}_{index:03d}"
            rows.append(
                {
                    "id": doc_id,
                    "category": spec.key,
                    "category_label": spec.label,
                    "input": f"inputs/{spec.key}/{doc_id}{spec.extension}",
                    "ground_truth": f"ground_truth/{spec.key}/{doc_id}.md",
                    "features": list(spec.features),
                    "language": "zh-Hans",
                    "status": "pending_source_document",
                }
            )
    return rows


def create_scaffold(output_dir: Path, count_per_category: int) -> Path:
    """Create input/ground-truth directories and a JSONL manifest."""
    if count_per_category < 1:
        raise ValueError("count-per-category must be >= 1")
    output_dir.mkdir(parents=True, exist_ok=True)
    for spec in CATEGORIES:
        (output_dir / "inputs" / spec.key).mkdir(parents=True, exist_ok=True)
        (output_dir / "ground_truth" / spec.key).mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "00_manifest.jsonl"
    rows = iter_manifest_entries(count_per_category)
    with manifest_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return manifest_path


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/dataset"))
    parser.add_argument("--count-per-category", type=int, default=25)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing manifest; source documents are never deleted",
    )
    return parser


def main() -> None:
    """CLI entry point."""
    args = build_parser().parse_args()
    manifest_path = args.output / "00_manifest.jsonl"
    if manifest_path.exists() and not args.force:
        raise FileExistsError(f"manifest already exists: {manifest_path} (use --force)")
    created = create_scaffold(args.output, args.count_per_category)
    print(f"created {len(CATEGORIES) * args.count_per_category} manifest rows at {created}")


if __name__ == "__main__":
    main()

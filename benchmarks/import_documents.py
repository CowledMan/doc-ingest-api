"""Register locally collected source documents into the benchmark manifest.

Real documents and ground truth stay outside version control. This script only
rewrites the manifest (also git-ignored) so collected files become
benchmark-ready without hand-editing JSONL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .scaffold_dataset import CATEGORIES, CategorySpec

STATUS_READY = "ready"
STATUS_PENDING = "pending_source_document"
UNKNOWN_SOURCE = "unknown"

_SPEC_BY_KEY: dict[str, CategorySpec] = {spec.key: spec for spec in CATEGORIES}


@dataclass(frozen=True)
class ImportReport:
    """Summary of one import run."""

    registered: tuple[str, ...]
    pending: tuple[str, ...]
    missing_ground_truth: tuple[str, ...]
    unknown_source: tuple[str, ...]


def _load_rows(manifest_path: Path) -> list[dict[str, object]]:
    """Load manifest rows, skipping blank lines."""
    rows: list[dict[str, object]] = []
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


def _row_key(row: dict[str, object], line_number: int) -> tuple[str, str]:
    """Return the (category, id) key used to match files to manifest rows."""
    category = row.get("category")
    doc_id = row.get("id")
    if not isinstance(category, str) or not isinstance(doc_id, str):
        raise ValueError(f"manifest row at line {line_number} lacks string 'category' and 'id'")
    return category, doc_id


def _iter_input_files(manifest_dir: Path) -> list[Path]:
    """Collect candidate documents below inputs/, skipping hidden and temp files."""
    inputs_dir = manifest_dir / "inputs"
    if not inputs_dir.is_dir():
        return []
    files = [
        path
        for path in sorted(inputs_dir.iterdir())
        if path.is_file() and not path.name.startswith((".", "~$"))
    ]
    for category_dir in sorted(path for path in inputs_dir.iterdir() if path.is_dir()):
        files.extend(
            path
            for path in sorted(category_dir.iterdir())
            if path.is_file() and not path.name.startswith((".", "~$"))
        )
    return files


def _sha256(path: Path) -> str:
    """Return the SHA256 digest for a file without loading it entirely."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_row(category: str, stem: str, input_relative: str, downloaded_at: str) -> dict[str, object]:
    """Build a manifest row for a document that had no placeholder row."""
    spec = _SPEC_BY_KEY[category]
    return {
        "id": stem,
        "category": category,
        "category_label": spec.label,
        "input": input_relative,
        "ground_truth": f"ground_truth/{category}/{stem}.md",
        "features": list(spec.features),
        "language": "zh-Hans",
        "status": STATUS_READY,
        "title": stem,
        "source_url": UNKNOWN_SOURCE,
        "sha256": "",
        "downloaded_at": downloaded_at,
    }


def _fill_file_metadata(
    row: dict[str, object], path: Path, input_relative: str, stem: str, downloaded_at: str
) -> bool:
    """Update file-derived metadata in place; return True when source is unknown."""
    row["input"] = input_relative
    row["status"] = STATUS_READY
    row["sha256"] = _sha256(path)
    if not row.get("title"):
        row["title"] = stem
    if not row.get("source_url"):
        row["source_url"] = UNKNOWN_SOURCE
    if not row.get("downloaded_at"):
        row["downloaded_at"] = downloaded_at
    return str(row.get("source_url", "")) == UNKNOWN_SOURCE


def _write_manifest(manifest_path: Path, rows: list[dict[str, object]]) -> None:
    """Atomically rewrite the manifest as JSONL."""
    payload = "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows)
    temp_path = manifest_path.with_name(manifest_path.name + ".tmp")
    temp_path.write_text(payload, encoding="utf-8")
    temp_path.replace(manifest_path)


def import_documents(
    manifest_path: Path,
    *,
    allow_new: bool = False,
    dry_run: bool = False,
    downloaded_at: str | None = None,
) -> ImportReport:
    """Match files under inputs/ to manifest rows and mark them ready.

    Rows matched by (category, filename stem) get their input path and SHA256
    refreshed. Unmatched files raise ValueError unless ``allow_new`` registers
    them as new rows in a known category. New rows start with
    ``source_url="unknown"`` so their provenance is easy to audit and backfill.
    """
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")
    if downloaded_at:
        date.fromisoformat(downloaded_at)
        import_date = downloaded_at
    else:
        import_date = date.today().isoformat()

    manifest_dir = manifest_path.parent
    rows = _load_rows(manifest_path)
    row_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for line_number, row in enumerate(rows, 1):
        key = _row_key(row, line_number)
        if key in row_by_key:
            raise ValueError(f"duplicate manifest key {key} at line {line_number}")
        row_by_key[key] = row

    registered: list[str] = []
    unmatched: list[str] = []
    unknown_source: set[str] = set()
    seen_stems: set[tuple[str, str]] = set()
    for path in _iter_input_files(manifest_dir):
        category = path.parent.name
        stem = path.stem
        if (category, stem) in seen_stems:
            raise ValueError(f"duplicate document stem in inputs: {category}/{stem}")
        seen_stems.add((category, stem))
        matched = row_by_key.get((category, stem))
        if matched is None and allow_new and category in _SPEC_BY_KEY:
            input_relative = path.relative_to(manifest_dir).as_posix()
            new_row = _new_row(category, stem, input_relative, import_date)
            _fill_file_metadata(new_row, path, input_relative, stem, import_date)
            rows.append(new_row)
            row_by_key[(category, stem)] = new_row
            registered.append(f"{category}/{stem}")
            if str(new_row["source_url"]) == UNKNOWN_SOURCE:
                unknown_source.add(f"{category}/{stem}")
            continue
        if matched is None:
            unmatched.append(path.relative_to(manifest_dir).as_posix())
            continue
        input_relative = path.relative_to(manifest_dir).as_posix()
        if _fill_file_metadata(matched, path, input_relative, stem, import_date):
            unknown_source.add(f"{category}/{stem}")
        registered.append(f"{category}/{stem}")

    if unmatched:
        hint = (
            "category directories must be known when --allow-new is used"
            if allow_new
            else "fix the file names or re-run with --allow-new"
        )
        raise ValueError(f"unmatched input files ({hint}): {', '.join(unmatched)}")

    pending: list[str] = []
    missing_ground_truth: list[str] = []
    for (category, doc_id), row in row_by_key.items():
        label = f"{category}/{doc_id}"
        if row.get("status") == STATUS_READY:
            ground_truth = str(row.get("ground_truth", ""))
            if not ground_truth or not (manifest_dir / ground_truth).is_file():
                missing_ground_truth.append(label)
        else:
            pending.append(label)

    report = ImportReport(
        registered=tuple(sorted(registered)),
        pending=tuple(sorted(pending)),
        missing_ground_truth=tuple(sorted(missing_ground_truth)),
        unknown_source=tuple(sorted(unknown_source)),
    )
    if not dry_run:
        _write_manifest(manifest_path, rows)
    return report


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/dataset/00_manifest.jsonl"),
        help="JSONL manifest path; document paths are relative to this file",
    )
    parser.add_argument(
        "--allow-new",
        action="store_true",
        help="register files without a placeholder row as new manifest rows",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the planned changes without rewriting the manifest",
    )
    parser.add_argument(
        "--downloaded-at",
        type=str,
        help="ISO date (YYYY-MM-DD) to use for new rows; defaults to today",
    )
    return parser


def main() -> None:
    """CLI entry point."""
    args = build_parser().parse_args()
    report = import_documents(
        args.manifest,
        allow_new=args.allow_new,
        dry_run=args.dry_run,
        downloaded_at=args.downloaded_at,
    )
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Tests for the benchmark manifest validator."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from benchmarks.validate_manifest import validate_manifest


def _write_dataset(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    """Create dataset files and manifest for the given rows."""
    manifest = tmp_path / "00_manifest.jsonl"
    written: list[dict[str, object]] = []
    for row in rows:
        input_path = tmp_path / str(row["input"])
        input_path.parent.mkdir(parents=True, exist_ok=True)
        payload = str(row["title"]).encode("utf-8")
        input_path.write_bytes(payload)
        complete = dict(row)
        complete["sha256"] = hashlib.sha256(payload).hexdigest()
        written.append(complete)
    with manifest.open("w", encoding="utf-8") as handle:
        for row in written:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return manifest


def _row(doc_id: str, category: str) -> dict[str, object]:
    """Return a complete valid manifest row."""
    return {
        "id": doc_id,
        "category": category,
        "category_label": "测试",
        "input": f"inputs/{category}/{doc_id}.pdf",
        "ground_truth": f"ground_truth/{category}/{doc_id}.md",
        "features": ["layout"],
        "language": "zh-Hans",
        "status": "ready",
        "title": doc_id,
        "source_url": "https://example.com",
        "sha256": "",
        "downloaded_at": "2026-10-10",
    }


def test_validate_manifest_ok(tmp_path: Path) -> None:
    rows = [
        _row(f"{category}_{i:03d}", category)
        for category in (
            "contracts",
            "academic_papers",
            "financial_reports",
            "scanned_documents",
            "presentations",
            "spreadsheets",
            "official_red_header",
            "formula_heavy",
        )
        for i in range(1, 3)
    ]
    manifest = _write_dataset(tmp_path, rows)

    report = validate_manifest(manifest, min_count=2)

    assert report["ok"] is True
    assert report["per_category"]["contracts"]["count"] == 2


def test_validate_manifest_detects_hash_mismatch(tmp_path: Path) -> None:
    rows = [_row("contracts_001", "contracts")]
    manifest = _write_dataset(tmp_path, rows)
    (tmp_path / "inputs" / "contracts" / "contracts_001.pdf").write_bytes(b"tampered")

    report = validate_manifest(manifest, min_count=1)

    assert report["ok"] is False
    assert any("sha256 mismatch" in error for error in report["errors"])


def test_validate_manifest_detects_missing_file(tmp_path: Path) -> None:
    rows = [_row("contracts_001", "contracts")]
    manifest = _write_dataset(tmp_path, rows)
    (tmp_path / "inputs" / "contracts" / "contracts_001.pdf").unlink()

    report = validate_manifest(manifest, min_count=1)

    assert report["ok"] is False
    assert any("missing input file" in error for error in report["errors"])


def test_validate_manifest_detects_below_minimum(tmp_path: Path) -> None:
    rows = [_row("contracts_001", "contracts")]
    manifest = _write_dataset(tmp_path, rows)

    report = validate_manifest(manifest, min_count=2)

    assert report["ok"] is False
    assert any("below minimum" in error for error in report["errors"])


def test_validate_manifest_detects_duplicate_sha256(tmp_path: Path) -> None:
    first = _row("contracts_001", "contracts")
    second = _row("contracts_002", "contracts")
    second["title"] = first["title"]  # same payload -> same hash
    manifest = _write_dataset(tmp_path, [first, second])

    report = validate_manifest(manifest, min_count=1)

    assert report["ok"] is False
    assert any("duplicate sha256" in error for error in report["errors"])
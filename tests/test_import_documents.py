"""Tests for the benchmark document import workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.import_documents import import_documents


def _write_manifest(path: Path, rows: list[dict[str, object]]) -> None:
    """Write a minimal JSONL manifest for tests."""
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _placeholder(doc_id: str, category: str, suffix: str = ".pdf") -> dict[str, object]:
    """Return one placeholder manifest row."""
    return {
        "id": doc_id,
        "category": category,
        "input": f"inputs/{category}/{doc_id}{suffix}",
        "ground_truth": f"ground_truth/{category}/{doc_id}.md",
        "status": "pending_source_document",
    }


def _read_manifest(path: Path) -> list[dict[str, object]]:
    """Read all rows from a manifest file."""
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_import_documents_marks_matching_rows_ready(tmp_path: Path) -> None:
    manifest = tmp_path / "00_manifest.jsonl"
    _write_manifest(
        manifest,
        [
            _placeholder("contracts_001", "contracts"),
            _placeholder("contracts_002", "contracts"),
        ],
    )
    inputs = tmp_path / "inputs" / "contracts"
    inputs.mkdir(parents=True)
    (inputs / "contracts_001.docx").write_bytes(b"fake")
    ground_truth_dir = tmp_path / "ground_truth" / "contracts"
    ground_truth_dir.mkdir(parents=True)
    (ground_truth_dir / "contracts_001.md").write_text("# GT", encoding="utf-8")

    report = import_documents(manifest)

    rows = _read_manifest(manifest)
    assert rows[0]["status"] == "ready"
    assert rows[0]["input"] == "inputs/contracts/contracts_001.docx"
    assert rows[1]["status"] == "pending_source_document"
    assert report.registered == ("contracts/contracts_001",)
    assert report.pending == ("contracts/contracts_002",)
    assert report.missing_ground_truth == ()


def test_import_documents_reports_missing_ground_truth(tmp_path: Path) -> None:
    manifest = tmp_path / "00_manifest.jsonl"
    _write_manifest(manifest, [_placeholder("contracts_001", "contracts")])
    inputs = tmp_path / "inputs" / "contracts"
    inputs.mkdir(parents=True)
    (inputs / "contracts_001.pdf").write_bytes(b"fake")

    report = import_documents(manifest)

    assert report.missing_ground_truth == ("contracts/contracts_001",)


def test_import_documents_fails_on_unmatched_file(tmp_path: Path) -> None:
    manifest = tmp_path / "00_manifest.jsonl"
    _write_manifest(manifest, [_placeholder("contracts_001", "contracts")])
    inputs = tmp_path / "inputs" / "contracts"
    inputs.mkdir(parents=True)
    (inputs / "unexpected_name.pdf").write_bytes(b"fake")

    with pytest.raises(ValueError, match="unexpected_name"):
        import_documents(manifest)


def test_import_documents_rejects_duplicate_stems(tmp_path: Path) -> None:
    manifest = tmp_path / "00_manifest.jsonl"
    _write_manifest(manifest, [_placeholder("contracts_001", "contracts")])
    inputs = tmp_path / "inputs" / "contracts"
    inputs.mkdir(parents=True)
    (inputs / "contracts_001.pdf").write_bytes(b"fake")
    (inputs / "contracts_001.docx").write_bytes(b"fake")

    with pytest.raises(ValueError, match="duplicate document stem"):
        import_documents(manifest)


def test_import_documents_appends_new_rows_with_allow_new(tmp_path: Path) -> None:
    manifest = tmp_path / "00_manifest.jsonl"
    _write_manifest(manifest, [_placeholder("contracts_001", "contracts")])
    inputs = tmp_path / "inputs" / "contracts"
    inputs.mkdir(parents=True)
    (inputs / "custom_sample.pdf").write_bytes(b"fake")

    report = import_documents(manifest, allow_new=True)

    rows = _read_manifest(manifest)
    appended = rows[-1]
    assert appended["id"] == "custom_sample"
    assert appended["category"] == "contracts"
    assert appended["category_label"] == "中文合同"
    assert appended["status"] == "ready"
    assert "tables" in appended["features"]
    assert report.registered == ("contracts/custom_sample",)


def test_import_documents_allow_new_requires_known_category(tmp_path: Path) -> None:
    manifest = tmp_path / "00_manifest.jsonl"
    _write_manifest(manifest, [_placeholder("contracts_001", "contracts")])
    inputs = tmp_path / "inputs" / "unknown_category"
    inputs.mkdir(parents=True)
    (inputs / "sample.pdf").write_bytes(b"fake")

    with pytest.raises(ValueError, match="unknown_category"):
        import_documents(manifest, allow_new=True)


def test_import_documents_dry_run_keeps_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "00_manifest.jsonl"
    _write_manifest(manifest, [_placeholder("contracts_001", "contracts")])
    before = manifest.read_text(encoding="utf-8")
    inputs = tmp_path / "inputs" / "contracts"
    inputs.mkdir(parents=True)
    (inputs / "contracts_001.pdf").write_bytes(b"fake")

    report = import_documents(manifest, dry_run=True)

    assert manifest.read_text(encoding="utf-8") == before
    assert report.registered == ("contracts/contracts_001",)
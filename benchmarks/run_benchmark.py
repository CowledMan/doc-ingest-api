"""Run the M0 three-engine benchmark over a JSONL manifest."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, TextIO

from .engine_adapters import ConversionError, get_adapter
from .metrics import score_document

DEFAULT_ENGINES = ("anydoc", "markitdown", "docling")


@dataclass(frozen=True)
class ManifestEntry:
    """One benchmark document and its human-reviewed ground truth."""

    id: str
    category: str
    input: Path
    ground_truth: Path
    features: tuple[str, ...]


def load_manifest(manifest_path: Path) -> list[ManifestEntry]:
    """Load a manifest with paths relative to the manifest directory."""
    entries: list[ManifestEntry] = []
    with manifest_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            try:
                entry = ManifestEntry(
                    id=str(raw["id"]),
                    category=str(raw["category"]),
                    input=manifest_path.parent / str(raw["input"]),
                    ground_truth=manifest_path.parent / str(raw["ground_truth"]),
                    features=tuple(raw.get("features", [])),
                )
            except (KeyError, TypeError) as exc:
                raise ValueError(f"invalid manifest row at line {line_number}: {exc}") from exc
            entries.append(entry)
    if not entries:
        raise ValueError(f"manifest has no rows: {manifest_path}")
    return entries


def _write_jsonl(handle: TextIO, payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")


def run_engine(
    engine_name: str,
    entries: list[ManifestEntry],
    manifest_path: Path,
    output_dir: Path,
    limit: int | None,
) -> dict[str, Any]:
    """Run one engine and write per-document outputs and JSONL results."""
    adapter = get_adapter(engine_name)
    engine_output_dir = output_dir / engine_name
    engine_output_dir.mkdir(parents=True, exist_ok=True)
    results_path = engine_output_dir / "results.jsonl"
    selected = entries if limit is None else entries[:limit]
    scores: list[float] = []
    table_scores: list[float] = []
    formula_scores: list[float] = []
    layout_scores: list[float] = []
    elapsed_seconds: list[float] = []
    failures: list[dict[str, str]] = []
    completed = 0

    with results_path.open("w", encoding="utf-8") as results_handle:
        for entry in selected:
            if not entry.input.is_file():
                failure = {"id": entry.id, "error": f"missing input: {entry.input}"}
                failures.append(failure)
                _write_jsonl(results_handle, {"id": entry.id, "status": "skipped", **failure})
                continue
            if not entry.ground_truth.is_file():
                failure = {"id": entry.id, "error": f"missing ground truth: {entry.ground_truth}"}
                failures.append(failure)
                _write_jsonl(results_handle, {"id": entry.id, "status": "skipped", **failure})
                continue
            started = time.perf_counter()
            try:
                result = adapter.convert(entry.input)
            except ConversionError as exc:
                failures.append({"id": entry.id, "error": str(exc)})
                _write_jsonl(results_handle, {"id": entry.id, "status": "failed", "error": str(exc)})
                continue
            elapsed = time.perf_counter() - started
            output_path = engine_output_dir / f"{entry.id}.md"
            output_path.write_text(result.markdown, encoding="utf-8")
            ground_truth = entry.ground_truth.read_text(encoding="utf-8")
            document_scores = score_document(ground_truth, result.markdown)
            scores.append(document_scores.overall)
            table_scores.append(document_scores.table_restoration)
            formula_scores.append(document_scores.formula_preservation)
            layout_scores.append(document_scores.layout_fidelity)
            elapsed_seconds.append(elapsed)
            completed += 1
            _write_jsonl(
                results_handle,
                {
                    "id": entry.id,
                    "status": "ok",
                    "category": entry.category,
                    "features": list(entry.features),
                    "output_path": str(output_path.relative_to(output_dir)),
                    "elapsed_seconds": elapsed,
                    "scores": document_scores.to_dict(),
                },
            )

    selected_count = len(selected)
    summary = {
        "engine": engine_name,
        "manifest": str(manifest_path),
        "selected_documents": selected_count,
        "completed": completed,
        "failed": len(failures),
        "skipped": selected_count - completed - len([f for f in failures if f["error"].startswith("missing")]),
        "average_scores": {
            "overall": mean(scores) if scores else 0.0,
            "table_restoration": mean(table_scores) if table_scores else 0.0,
            "formula_preservation": mean(formula_scores) if formula_scores else 0.0,
            "layout_fidelity": mean(layout_scores) if layout_scores else 0.0,
        },
        "average_elapsed_seconds": mean(elapsed_seconds) if elapsed_seconds else 0.0,
        "failures": failures,
    }
    (engine_output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/dataset/00_manifest.jsonl"),
        help="JSONL manifest path; row paths are relative to this file",
    )
    parser.add_argument(
        "--engines",
        nargs="+",
        choices=DEFAULT_ENGINES,
        default=DEFAULT_ENGINES,
        help="engines to benchmark",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/results"),
        help="benchmark output directory",
    )
    parser.add_argument("--limit", type=int, help="evaluate only the first N manifest rows")
    return parser


def main() -> None:
    """CLI entry point."""
    args = build_parser().parse_args()
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be >= 1")
    entries = load_manifest(args.manifest)
    args.output.mkdir(parents=True, exist_ok=True)
    summaries = [
        run_engine(engine, entries, args.manifest, args.output, args.limit)
        for engine in args.engines
    ]
    report_path = args.output / "comparison.json"
    report_path.write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summaries, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

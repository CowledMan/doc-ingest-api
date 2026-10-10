"""Internal conversion worker: one engine, many docs, results written per doc.

Invoked by benchmarks.engine_comparison as a subprocess so engine imports and
model loads happen once per batch while the parent keeps timeout control.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def _convert_anydoc(input_path: Path) -> str:
    """Convert with firecrawl-anydoc."""
    import anydoc

    markdown = anydoc.to_markdown(str(input_path))
    if not isinstance(markdown, str):
        raise RuntimeError("anydoc returned a non-string result")
    return str(markdown)


def _convert_markitdown(input_path: Path) -> str:
    """Convert with Microsoft MarkItDown."""
    from markitdown import MarkItDown

    result = MarkItDown().convert_local(input_path)
    markdown = result.markdown
    if not isinstance(markdown, str):
        raise RuntimeError("markitdown returned a non-string result")
    return markdown


def _convert_docling(input_path: Path) -> str:
    """Convert with Docling."""
    from docling.document_converter import DocumentConverter

    markdown: str = DocumentConverter().convert(input_path).document.export_to_markdown()
    return markdown


_CONVERTERS = {
    "anydoc": _convert_anydoc,
    "markitdown": _convert_markitdown,
    "docling": _convert_docling,
}


def main(argv: list[str]) -> int:
    """Process the JSONL task file and write one result JSON per document."""
    if len(argv) != 4:
        print("usage: _convert_worker.py <engine> <docs.jsonl> <results_dir>", file=sys.stderr)
        return 2
    engine, docs_path, results_dir = argv[1], Path(argv[2]), Path(argv[3])
    converter = _CONVERTERS.get(engine)
    if converter is None:
        print(f"unknown engine: {engine}", file=sys.stderr)
        return 2
    results_dir.mkdir(parents=True, exist_ok=True)
    docs = [
        json.loads(line)
        for line in docs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for doc in docs:
        doc_id = str(doc["id"])
        input_path = Path(str(doc["input"]))
        output_path = Path(str(doc["output"]))
        started = time.perf_counter()
        try:
            markdown = converter(input_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(markdown, encoding="utf-8")
            record = {
                "id": doc_id,
                "status": "ok",
                "elapsed_seconds": time.perf_counter() - started,
                "output_chars": len(markdown),
            }
        except Exception as exc:  # noqa: BLE001 - record any conversion failure
            record = {
                "id": doc_id,
                "status": "error",
                "elapsed_seconds": time.perf_counter() - started,
                "output_chars": 0,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        result_path = results_dir / f"{doc_id}.json"
        temp_path = result_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
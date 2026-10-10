"""Isolated engine conversion worker used by the production parser adapters."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .confidence import estimate_confidence


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


_CONVERTERS: dict[str, Callable[[Path], str]] = {
    "anydoc": _convert_anydoc,
    "markitdown": _convert_markitdown,
    "docling": _convert_docling,
}


def main(argv: list[str]) -> int:
    """Convert one document and emit a JSON result on stdout."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    if len(argv) != 3:
        print("usage: engine_worker <engine> <file>", file=sys.stderr)
        return 2
    engine = argv[1]
    input_path = Path(argv[2])
    converter = _CONVERTERS.get(engine)
    if converter is None:
        print(f"unknown engine: {engine}", file=sys.stderr)
        return 2

    started = time.perf_counter()
    try:
        markdown = converter(input_path)
    except Exception as exc:  # noqa: BLE001 - serialize any parser failure to the parent
        print(
            json.dumps(
                {
                    "markdown": "",
                    "confidence": 0.0,
                    "metadata": {},
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "markdown": markdown,
                "confidence": estimate_confidence(markdown),
                "metadata": {"elapsed_seconds": time.perf_counter() - started},
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Smoke test for Docling."""

from importlib.metadata import version
from pathlib import Path
from tempfile import NamedTemporaryFile

from docling.document_converter import DocumentConverter


def main() -> None:
    """Convert a small local text file and verify Markdown export."""
    with NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as handle:
        handle.write("hello doc ingest\n")
        path = Path(handle.name)
    try:
        result = DocumentConverter().convert(path)
        markdown = result.document.export_to_markdown()
        assert isinstance(markdown, str)
        assert "hello doc ingest" in markdown
    finally:
        path.unlink(missing_ok=True)
    print(f"docling {version('docling')}: ok")


if __name__ == "__main__":
    main()

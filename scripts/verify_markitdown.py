"""Smoke test for Microsoft MarkItDown."""

from importlib.metadata import version
from pathlib import Path
from tempfile import NamedTemporaryFile

from markitdown import MarkItDown


def main() -> None:
    """Convert a small local CSV file and verify the Markdown table."""
    with NamedTemporaryFile("w+b", suffix=".csv", delete=False) as handle:
        handle.write(b"column_a,column_b\n1,2\n3,4\n")
        path = Path(handle.name)
    try:
        result = MarkItDown().convert_local(path)
        markdown = result.markdown
        assert isinstance(markdown, str)
        assert "| column_a | column_b |" in markdown
        assert "| 1 | 2 |" in markdown
    finally:
        path.unlink(missing_ok=True)
    print(f"markitdown {version('markitdown')}: ok")


if __name__ == "__main__":
    main()

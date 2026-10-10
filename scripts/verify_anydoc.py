"""Smoke test for the firecrawl-anydoc Python binding."""

from importlib.metadata import version

import anydoc


def main() -> None:
    """Convert a small CSV payload and verify the Markdown table."""
    data = b"column_a,column_b\n1,2\n3,4\n"
    markdown = anydoc.to_markdown_bytes(data, "csv")
    assert isinstance(markdown, str)
    assert "| column_a | column_b |" in markdown
    assert "| 1 | 2 |" in markdown
    print(f"anydoc {version('firecrawl-anydoc')}: ok")


if __name__ == "__main__":
    main()

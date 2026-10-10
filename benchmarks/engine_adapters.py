"""Engine adapters used by the M0 benchmark framework."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


class ConversionError(RuntimeError):
    """Raised when an engine cannot produce Markdown."""


@dataclass(frozen=True)
class ConversionResult:
    """Normalized result returned by every engine adapter."""

    markdown: str
    metadata: dict[str, str]


class EngineAdapter(ABC):
    """Common interface for benchmark engines."""

    name: ClassVar[str]
    install_hint: ClassVar[str]

    @abstractmethod
    def convert(self, input_path: Path) -> ConversionResult:
        """Convert a local document to Markdown."""
        raise NotImplementedError


class AnydocAdapter(EngineAdapter):
    """Adapter for the firecrawl-anydoc Python binding."""

    name = "anydoc"
    install_hint = "uv run --with firecrawl-anydoc python -m benchmarks.run_benchmark"

    def convert(self, input_path: Path) -> ConversionResult:
        try:
            import anydoc
        except ImportError as exc:
            raise ConversionError(f"anydoc is not installed. {self.install_hint}") from exc
        try:
            markdown = anydoc.to_markdown(str(input_path))
        except Exception as exc:
            raise ConversionError(f"anydoc failed: {exc}") from exc
        return ConversionResult(markdown=markdown, metadata={"engine": self.name})


class MarkitdownAdapter(EngineAdapter):
    """Adapter for Microsoft MarkItDown."""

    name = "markitdown"
    install_hint = "uv run --with 'markitdown[all]' python -m benchmarks.run_benchmark"

    def convert(self, input_path: Path) -> ConversionResult:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise ConversionError(f"markitdown is not installed. {self.install_hint}") from exc
        try:
            result = MarkItDown().convert_local(input_path)
            markdown = result.markdown
        except Exception as exc:
            raise ConversionError(f"markitdown failed: {exc}") from exc
        if not isinstance(markdown, str):
            raise ConversionError("markitdown returned a non-string result")
        return ConversionResult(markdown=markdown, metadata={"engine": self.name})


class DoclingAdapter(EngineAdapter):
    """Adapter for Docling."""

    name = "docling"
    install_hint = "uv run --with docling python -m benchmarks.run_benchmark"

    def convert(self, input_path: Path) -> ConversionResult:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as exc:
            raise ConversionError(f"docling is not installed. {self.install_hint}") from exc
        try:
            result = DocumentConverter().convert(input_path)
            markdown = result.document.export_to_markdown()
        except Exception as exc:
            raise ConversionError(f"docling failed: {exc}") from exc
        return ConversionResult(markdown=markdown, metadata={"engine": self.name})


_ADAPTERS: dict[str, type[EngineAdapter]] = {
    AnydocAdapter.name: AnydocAdapter,
    MarkitdownAdapter.name: MarkitdownAdapter,
    DoclingAdapter.name: DoclingAdapter,
}


def get_adapter(name: str) -> EngineAdapter:
    """Return an engine adapter by its stable name."""
    try:
        return _ADAPTERS[name]()
    except KeyError as exc:
        choices = ", ".join(sorted(_ADAPTERS))
        raise ValueError(f"unknown engine {name!r}; choose one of: {choices}") from exc

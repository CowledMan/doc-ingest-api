"""Tests for the Mistral OCR adapter."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from doc_ingest_api.mistral_ocr import MistralOcrParser
from doc_ingest_api.parsers import OcrNotConfiguredError, ParserError


def _make_png_bytes() -> bytes:
    """Return a minimal 10x10 PNG."""
    import struct
    import zlib

    png = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 10, 10, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    png += struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)
    raw = b"".join(b"\x00" + b"\x80\x00\x00" * 10 for _ in range(10))
    compressed = zlib.compress(raw)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    png += struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", idat_crc)
    png += struct.pack(">I", 0) + b"IEND" + struct.pack(">I", idat_crc)
    return png


def _mistral_body(markdowns: list[str]) -> dict[str, Any]:
    """Build a mock /v1/ocr response body."""
    return {
        "model": "mistral-ocr-latest",
        "pages": [
            {"index": i, "markdown": md, "images": []} for i, md in enumerate(markdowns)
        ],
        "usage_info": {"pages_processed": len(markdowns)},
    }


def _mock_response(data: dict[str, Any], status: int = 200) -> httpx.Response:
    """Create an httpx.Response with a request set so raise_for_status works."""
    response = httpx.Response(status_code=status, json=data)
    response.request = httpx.Request("POST", "https://api.mistral.test/v1/ocr")
    return response


class TestOcrNotConfigured:
    def test_no_key(self, tmp_path: Path) -> None:
        parser = MistralOcrParser(api_key="")
        with pytest.raises(OcrNotConfiguredError):
            parser.parse(tmp_path / "a.pdf")


class TestImageParse:
    def test_png(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        png_bytes = _make_png_bytes()
        img.write_bytes(png_bytes)

        captured: dict[str, Any] = {}

        def fake_post(url: str, **kw: Any) -> httpx.Response:
            captured["url"] = url
            captured["json"] = kw.get("json", {})
            captured["headers"] = kw.get("headers", {})
            return _mock_response(_mistral_body(["hello OCR"]))

        monkeypatch.setattr("doc_ingest_api.mistral_ocr.httpx.post", fake_post)
        parser = MistralOcrParser(api_key="mk", timeout_seconds=30.0)
        result = parser.parse(img)

        assert result.markdown == "<!-- page 1 -->\nhello OCR"
        assert result.engine_used == "ocr"
        assert result.metadata == {"engine": "mistral_ocr", "pages": 1}
        assert result.confidence == pytest.approx(0.9)

        doc = captured["json"]["document"]
        assert doc["type"] == "image_url"
        assert doc["image_url"].startswith("data:image/png;base64,")
        assert base64.b64encode(png_bytes).decode("ascii") in doc["image_url"]
        assert captured["json"]["model"] == "mistral-ocr-latest"
        assert captured["headers"]["Authorization"] == "Bearer mk"
        assert captured["url"] == "https://api.mistral.ai/v1/ocr"

    def test_jpg_mime(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)

        captured: dict[str, Any] = {}

        def fake_post(url: str, **kw: Any) -> httpx.Response:
            captured["json"] = kw.get("json", {})
            return _mock_response(_mistral_body(["jpg text"]))

        monkeypatch.setattr("doc_ingest_api.mistral_ocr.httpx.post", fake_post)
        parser = MistralOcrParser(api_key="mk")
        result = parser.parse(img)
        assert result.markdown.endswith("jpg text")
        assert captured["json"]["document"]["image_url"].startswith("data:image/jpeg;base64,")

    def test_unsupported(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x")
        parser = MistralOcrParser(api_key="mk")
        with pytest.raises(ParserError, match="does not support"):
            parser.parse(f)


class TestPdfParse:
    def test_pdf_sent_whole(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pdf_file = tmp_path / "multi.pdf"
        pdf_bytes = b"%PDF-fake"
        pdf_file.write_bytes(pdf_bytes)

        captured: dict[str, Any] = {}

        def fake_post(url: str, **kw: Any) -> httpx.Response:
            captured["json"] = kw.get("json", {})
            return _mock_response(_mistral_body(["page1 text", "page2 text"]))

        monkeypatch.setattr("doc_ingest_api.mistral_ocr.httpx.post", fake_post)
        parser = MistralOcrParser(api_key="mk")
        result = parser.parse(pdf_file)

        doc = captured["json"]["document"]
        assert doc["type"] == "document_url"
        assert doc["document_url"].startswith("data:application/pdf;base64,")
        assert base64.b64encode(pdf_bytes).decode("ascii") in doc["document_url"]

        assert result.engine_used == "ocr"
        assert result.metadata["pages"] == 2
        assert "<!-- page 1 -->\npage1 text" in result.markdown
        assert "<!-- page 2 -->\npage2 text" in result.markdown


class TestNetworkErrors:
    def test_connection_error_is_parser_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())

        def fail(url: str, **kw: Any) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr("doc_ingest_api.mistral_ocr.httpx.post", fail)
        parser = MistralOcrParser(api_key="mk")
        with pytest.raises(ParserError, match="connection refused"):
            parser.parse(img)

    def test_timeout_is_parser_error_not_timeout_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Router falls through on ParserError; timeout must NOT be ParserTimeoutError."""
        from doc_ingest_api.parsers import ParserTimeoutError

        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())

        def fail(url: str, **kw: Any) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        monkeypatch.setattr("doc_ingest_api.mistral_ocr.httpx.post", fail)
        parser = MistralOcrParser(api_key="mk", timeout_seconds=30.0)
        with pytest.raises(ParserError) as exc_info:
            parser.parse(img)
        assert not isinstance(exc_info.value, ParserTimeoutError)
        assert "timed out" in str(exc_info.value)


class TestApiErrors:
    def test_http_error_includes_detail(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())
        monkeypatch.setattr(
            "doc_ingest_api.mistral_ocr.httpx.post",
            lambda url, **kw: _mock_response({"message": "Unauthorized"}, status=401),
        )
        parser = MistralOcrParser(api_key="bad")
        with pytest.raises(ParserError, match="HTTP 401.*Unauthorized"):
            parser.parse(img)

    def test_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())

        def bad_json(url: str, **kw: Any) -> httpx.Response:
            resp = httpx.Response(status_code=200, content=b"not json")
            resp.request = httpx.Request("POST", url)
            return resp

        monkeypatch.setattr("doc_ingest_api.mistral_ocr.httpx.post", bad_json)
        parser = MistralOcrParser(api_key="mk")
        with pytest.raises(ParserError, match="invalid JSON"):
            parser.parse(img)

    def test_empty_pages(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())
        monkeypatch.setattr(
            "doc_ingest_api.mistral_ocr.httpx.post",
            lambda url, **kw: _mock_response({"model": "mistral-ocr-latest", "pages": []}),
        )
        parser = MistralOcrParser(api_key="mk")
        with pytest.raises(ParserError, match="no pages"):
            parser.parse(img)


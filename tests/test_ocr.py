"""Tests for the Aliyun OCR adapter."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from doc_ingest_api.ocr import AliyunOcrParser
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
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    png += struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    return png


def _ocr_body(content: str = "recognized", score: float = 0.95) -> dict[str, Any]:
    """Build a mock RecognizeGeneral response body."""
    prism = json.dumps({"blocks": [{"words": [{"text": "w", "score": score}]}]})
    return {"RequestId": "r", "Data": {"Content": content, "PrismInfo": prism}}


def _mock_response(data: dict[str, Any], status: int = 200) -> httpx.Response:
    """Create an httpx.Response with a request set so raise_for_status works."""
    response = httpx.Response(status_code=status, json=data)
    response.request = httpx.Request("POST", "https://ocr-api.test/")
    return response


class TestOcrNotConfigured:
    def test_no_key(self, tmp_path: Path) -> None:
        parser = AliyunOcrParser(access_key_id="", access_key_secret="")
        with pytest.raises(OcrNotConfiguredError):
            parser.parse(tmp_path / "a.pdf")

    def test_no_secret(self, tmp_path: Path) -> None:
        parser = AliyunOcrParser(access_key_id="id", access_key_secret="")
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
            captured["content"] = kw.get("content", "")
            return _mock_response(_ocr_body("hello OCR"))

        monkeypatch.setattr("doc_ingest_api.ocr.httpx.post", fake_post)
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s", timeout_seconds=30.0)
        result = parser.parse(img)

        assert result.markdown == "hello OCR"
        assert result.engine_used == "ocr"
        assert result.metadata["pages"] == 1
        assert result.confidence == pytest.approx(0.95)
        assert base64.b64encode(png_bytes).decode("ascii")[:10] in captured["content"]
        assert "ocr-api.cn-hangzhou.aliyuncs.com" in captured["url"]

    def test_jpg(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)

        monkeypatch.setattr(
            "doc_ingest_api.ocr.httpx.post",
            lambda url, **kw: _mock_response(_ocr_body("jpg text")),
        )
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s")
        result = parser.parse(img)
        assert result.markdown == "jpg text"

    def test_unsupported(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x")
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s")
        with pytest.raises(ParserError, match="does not support"):
            parser.parse(f)


class TestNetworkErrors:
    def test_connection_error_is_parser_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())

        def fail(url: str, **kw: Any) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr("doc_ingest_api.ocr.httpx.post", fail)
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s")
        with pytest.raises(ParserError, match="connection refused"):
            parser.parse(img)

    def test_timeout_is_parser_error_not_timeout_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Router falls through on ParserError; timeout must NOT be ParserTimeoutError."""
        from doc_ingest_api.parsers import ParserTimeoutError

        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())

        def fail(url: str, **kw: Any) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        monkeypatch.setattr("doc_ingest_api.ocr.httpx.post", fail)
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s", timeout_seconds=30.0)
        with pytest.raises(ParserError) as exc_info:
            parser.parse(img)
        assert not isinstance(exc_info.value, ParserTimeoutError)
        assert "timed out" in str(exc_info.value)


class TestApiErrors:
    def test_error_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())
        monkeypatch.setattr(
            "doc_ingest_api.ocr.httpx.post",
            lambda url, **kw: _mock_response({"Code": "InvalidAccessKeyId", "Message": "bad key"}),
        )
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s")
        with pytest.raises(ParserError, match="InvalidAccessKeyId"):
            parser.parse(img)

    def test_invalid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())

        def bad_json(url: str, **kw: Any) -> httpx.Response:
            resp = httpx.Response(status_code=200, content=b"not json")
            resp.request = httpx.Request("POST", url)
            return resp

        monkeypatch.setattr("doc_ingest_api.ocr.httpx.post", bad_json)
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s")
        with pytest.raises(ParserError, match="invalid JSON"):
            parser.parse(img)


class TestPdfParse:
    def test_multi_page_pdf(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mock PDF rendering to isolate OCR logic from pypdfium2."""
        pdf_file = tmp_path / "multi.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        page_b64 = [base64.b64encode(_make_png_bytes()).decode("ascii")]
        responses = [_ocr_body("page1 text", 0.95), _ocr_body("page2 text", 0.85)]
        call_idx = [0]

        def fake_post(url: str, **kw: Any) -> httpx.Response:
            r = _mock_response(responses[call_idx[0]])
            call_idx[0] += 1
            return r

        monkeypatch.setattr("doc_ingest_api.ocr.httpx.post", fake_post)
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s")
        parser._pdf_to_base64_images = lambda f: page_b64 * 2  # type: ignore[method-assign]
        result = parser.parse(pdf_file)

        assert result.engine_used == "ocr"
        assert result.metadata["pages"] == 2
        assert "<!-- page 1 -->" in result.markdown
        assert "<!-- page 2 -->" in result.markdown
        assert "page1 text" in result.markdown
        assert "page2 text" in result.markdown
        assert result.confidence == pytest.approx(0.9)

    def test_empty_pdf_no_pages(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        pdf_file = tmp_path / "empty.pdf"
        pdf_file.write_bytes(b"%PDF-fake")

        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s")
        parser._pdf_to_base64_images = lambda f: []  # type: ignore[method-assign]
        with pytest.raises(ParserError, match="no pages"):
            parser.parse(pdf_file)


class TestConfidence:
    def test_default_when_no_prism(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())
        body = {"RequestId": "r", "Data": {"Content": "text only"}}
        monkeypatch.setattr(
            "doc_ingest_api.ocr.httpx.post", lambda url, **kw: _mock_response(body)
        )
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s")
        result = parser.parse(img)
        assert result.confidence == pytest.approx(0.9)

    def test_malformed_prism(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        img = tmp_path / "s.png"
        img.write_bytes(_make_png_bytes())
        body = {"RequestId": "r", "Data": {"Content": "text", "PrismInfo": "bad json"}}
        monkeypatch.setattr(
            "doc_ingest_api.ocr.httpx.post", lambda url, **kw: _mock_response(body)
        )
        parser = AliyunOcrParser(access_key_id="k", access_key_secret="s")
        result = parser.parse(img)
        assert result.confidence == pytest.approx(0.9)


class TestSignature:
    def test_rpc_signature_deterministic(self) -> None:
        from doc_ingest_api.ocr import _rpc_signature

        params = {
            "Action": "Test",
            "Version": "2021-07-07",
            "Format": "JSON",
            "AccessKeyId": "key",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": "nonce123",
            "Timestamp": "2026-01-01T00:00:00Z",
        }
        sig1 = _rpc_signature(params, "secret")
        sig2 = _rpc_signature(params, "secret")
        assert sig1 == sig2
        assert len(sig1) > 0
        # Different secret -> different signature
        sig3 = _rpc_signature(params, "other_secret")
        assert sig3 != sig1

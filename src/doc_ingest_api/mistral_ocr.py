"""Mistral OCR adapter using the official OCR REST API via ``httpx``.

Calls ``POST /v1/ocr`` with a base64 data URL. PDFs are sent whole (Mistral
handles pagination server-side); images are sent as ``image_url`` documents.
``httpx`` is already a project dependency, so no extra SDK is needed.

Response pages contain Markdown; we join them with page markers to keep the
output contract identical to the Aliyun adapter.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx

from .exceptions import OcrNotConfiguredError, ParserError, ParseResult

SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"})

_IMAGE_MIME: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


class MistralOcrParser:
    """Pay-as-you-go Mistral OCR adapter for scanned documents.

    Raises:
        OcrNotConfiguredError: when the API key is empty.
        ParserError: when the API call fails, times out, or returns invalid
            data.  The router catches ``ParserError`` and falls through to
            the next parser (docling), avoiding a 502 response.
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "mistral-ocr-latest",
        endpoint: str = "https://api.mistral.ai/v1/ocr",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds

    def parse(self, file: Path) -> ParseResult:
        """Parse a scanned document (PDF or image) via Mistral OCR."""
        if not self._api_key:
            raise OcrNotConfiguredError(
                "OCR is required for this document, but Mistral OCR "
                "credentials are not configured"
            )
        suffix = file.suffix.lower()
        if suffix == ".pdf":
            document: dict[str, str] = {
                "type": "document_url",
                "document_url": "data:application/pdf;base64,"
                + base64.b64encode(file.read_bytes()).decode("ascii"),
            }
        elif suffix in _IMAGE_MIME:
            document = {
                "type": "image_url",
                "image_url": f"data:{_IMAGE_MIME[suffix]};base64,"
                + base64.b64encode(file.read_bytes()).decode("ascii"),
            }
        else:
            raise ParserError(f"OCR does not support file type: {suffix}")
        return self._parse_document(document)

    def _parse_document(self, document: dict[str, str]) -> ParseResult:
        """Send one OCR request and assemble the page results."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": self._model, "document": document}
        try:
            response = httpx.post(
                self._endpoint,
                json=payload,
                headers=headers,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ParserError(
                f"Mistral OCR timed out after {self._timeout_seconds:.0f} seconds"
            ) from exc
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = str(exc.response.json().get("message", ""))[:200]
            except (json.JSONDecodeError, ValueError):
                detail = exc.response.text[:200]
            suffix_detail = f": {detail}" if detail else ""
            raise ParserError(
                f"Mistral OCR returned HTTP {exc.response.status_code}{suffix_detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ParserError(f"Mistral OCR request failed: {exc}") from exc

        body = self._parse_response(response)
        pages = body.get("pages")
        if not isinstance(pages, list) or not pages:
            raise ParserError("Mistral OCR response contains no pages")

        sections: list[str] = []
        for position, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                raise ParserError("Mistral OCR page entry is not an object")
            content = str(page.get("markdown", ""))
            sections.append(f"<!-- page {position} -->\n{content}")
        markdown = "\n\n".join(sections)
        page_count = len(pages)
        return ParseResult(
            markdown=markdown,
            confidence=0.9,
            metadata={"engine": "mistral_ocr", "pages": page_count},
            engine_used="ocr",
        )

    def _parse_response(self, response: httpx.Response) -> dict[str, Any]:
        """Validate and decode the JSON response body."""
        try:
            body: dict[str, Any] = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ParserError("Mistral OCR returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ParserError("Mistral OCR response is not a JSON object")
        return body


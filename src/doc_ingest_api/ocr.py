"""Aliyun OCR adapter using hand-signed RPC REST calls via ``httpx``.

Uses ``RecognizeGeneral`` from the Aliyun OCR API (version 2021-07-07).
The hand-signed approach avoids the heavy ``alibabacloud`` SDK dependency
chain (``alibabacloud_tea``, ``alibabacloud_openplatform``, etc.) while
``httpx`` is already a project dependency.

PDF pages are rendered to PNG via ``pypdfium2`` (already in the dependency
tree via ``docling``), OCR-ed page by page, and joined with page markers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import httpx
import pypdfium2 as pdfium

from .exceptions import OcrNotConfiguredError, ParserError, ParseResult

SUPPORTED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"})
OCR_API_VERSION = "2021-07-07"


def _pop_encode(value: str) -> str:
    """Percent-encode per Alibaba Cloud POP spec (RFC 3986 unreserved chars)."""
    return urllib.parse.quote(value, safe="-_.~")


def _rpc_signature(
    params: dict[str, str], access_key_secret: str
) -> str:
    """Compute HMAC-SHA1 signature for Alibaba Cloud RPC style requests."""
    sorted_params = sorted(params.items())
    canonical_query = "&".join(
        f"{_pop_encode(key)}={_pop_encode(val)}" for key, val in sorted_params
    )
    string_to_sign = f"POST&{_pop_encode('/')}&{_pop_encode(canonical_query)}"
    key = (access_key_secret + "&").encode("utf-8")
    digest = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


class AliyunOcrParser:
    """Pay-as-you-go Aliyun OCR adapter for scanned documents.

    Raises:
        OcrNotConfiguredError: when credentials are empty.
        ParserError: when the API call fails, times out, or returns invalid
            data.  The router catches ``ParserError`` and falls through to
            the next parser (docling), avoiding a 502 response.
    """

    def __init__(
        self,
        access_key_id: str = "",
        access_key_secret: str = "",
        endpoint: str = "ocr-api.cn-hangzhou.aliyuncs.com",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._endpoint = endpoint
        self._timeout_seconds = timeout_seconds

    def parse(self, file: Path) -> ParseResult:
        """Parse a scanned document (PDF or image) via Aliyun OCR."""
        if not self._access_key_id or not self._access_key_secret:
            raise OcrNotConfiguredError(
                "OCR is required for this document, but Aliyun OCR "
                "credentials are not configured"
            )
        suffix = file.suffix.lower()
        if suffix == ".pdf":
            return self._parse_pdf(file)
        if suffix in SUPPORTED_IMAGE_SUFFIXES:
            return self._parse_image(file)
        raise ParserError(f"OCR does not support file type: {suffix}")

    def _parse_pdf(self, file: Path) -> ParseResult:
        """Render each PDF page to PNG, OCR page-by-page, join results."""
        images_b64 = self._pdf_to_base64_images(file)
        if not images_b64:
            raise ParserError("PDF has no pages to OCR")
        pages: list[str] = []
        confidences: list[float] = []
        for index, image_b64 in enumerate(images_b64, start=1):
            content, confidence = self._ocr_base64_image(image_b64)
            pages.append(f"<!-- page {index} -->\n{content}")
            confidences.append(confidence)
        markdown = "\n\n".join(pages)
        average = sum(confidences) / len(confidences)
        return ParseResult(
            markdown=markdown,
            confidence=average,
            metadata={"engine": "aliyun_ocr", "pages": len(images_b64)},
            engine_used="ocr",
        )

    def _parse_image(self, file: Path) -> ParseResult:
        """OCR a single image file directly."""
        image_b64 = base64.b64encode(file.read_bytes()).decode("ascii")
        content, confidence = self._ocr_base64_image(image_b64)
        return ParseResult(
            markdown=content,
            confidence=confidence,
            metadata={"engine": "aliyun_ocr", "pages": 1},
            engine_used="ocr",
        )

    def _pdf_to_base64_images(self, file: Path) -> list[str]:
        """Render PDF pages to base64-encoded PNG strings via pypdfium2."""
        results: list[str] = []
        pdf = pdfium.PdfDocument(str(file))
        try:
            for page in pdf:
                bitmap = page.render(scale=2.0)
                buffer = io.BytesIO()
                bitmap.to_pil().save(buffer, format="PNG")
                results.append(base64.b64encode(buffer.getvalue()).decode("ascii"))
                bitmap.close()
        finally:
            pdf.close()
        return results

    def _ocr_base64_image(self, image_b64: str) -> tuple[str, float]:
        """Send a base64 image to Aliyun RecognizeGeneral, return (text, conf)."""
        params: dict[str, str] = {
            "Action": "RecognizeGeneral",
            "Version": OCR_API_VERSION,
            "Format": "JSON",
            "AccessKeyId": self._access_key_id,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex,
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ImageURL": image_b64,
        }
        params["Signature"] = _rpc_signature(params, self._access_key_secret)

        url = f"https://{self._endpoint}/"
        body = "&".join(
            f"{_pop_encode(k)}={_pop_encode(v)}" for k, v in sorted(params.items())
        )
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        try:
            response = httpx.post(
                url, content=body, headers=headers, timeout=self._timeout_seconds
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ParserError(
                f"Aliyun OCR timed out after {self._timeout_seconds:.0f} seconds"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ParserError(
                f"Aliyun OCR returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ParserError(f"Aliyun OCR request failed: {exc}") from exc

        return self._parse_response(response)

    def _parse_response(self, response: httpx.Response) -> tuple[str, float]:
        """Extract recognized text and confidence from the API response."""
        try:
            body: dict[str, Any] = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ParserError("Aliyun OCR returned invalid JSON") from exc

        if body.get("Code"):
            raise ParserError(
                f"Aliyun OCR API error: {body['Code']}: {body.get('Message', '')}"
            )
        data = body.get("Data")
        if not isinstance(data, dict):
            raise ParserError("Aliyun OCR response missing Data object")
        content = str(data.get("Content", ""))
        confidence = self._extract_confidence(data)
        return content, confidence

    def _extract_confidence(self, data: dict[str, Any]) -> float:
        """Try to derive average word-level confidence from PrismInfo."""
        prism_raw = data.get("PrismInfo", "")
        if isinstance(prism_raw, str) and prism_raw:
            try:
                prism: dict[str, Any] = json.loads(prism_raw)
                scores: list[float] = []
                for block in prism.get("blocks", []):
                    for word in block.get("words", []):
                        score = word.get("score")
                        if isinstance(score, (int, float)):
                            scores.append(float(score))
                if scores:
                    return sum(scores) / len(scores)
            except (json.JSONDecodeError, TypeError):
                pass
        return 0.9

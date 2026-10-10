"""HTTP client for the Document Ingest API."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .models import FileInput, Job, JobCreated, JobResult, JobStatus, ParseResult


class DocIngestError(RuntimeError):
    """Base error for the Document Ingest SDK."""


@dataclass(frozen=True)
class DocIngestAPIError(DocIngestError):
    """Non-2xx response from the Document Ingest gateway."""

    status_code: int
    detail: str

    def __str__(self) -> str:
        """Return a readable HTTP error message."""
        return f"{self.status_code}: {self.detail}"


class JobTimeoutError(DocIngestError):
    """Raised when an asynchronous job does not finish before the deadline."""


class DocIngestClient:
    """Small, typed HTTP client for the Document Ingest gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create a client configured for one gateway deployment."""
        if not base_url:
            raise ValueError("base_url must not be empty")
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key},
            timeout=timeout_seconds,
            transport=transport,
        )

    def parse(self, file: FileInput, *, category: str | None = None) -> ParseResult:
        """Parse a local file or in-memory document synchronously."""
        response = self._client.post(
            "/v1/parse",
            files=self._multipart_file(file),
            data=self._form_data(category=category),
        )
        self._raise_for_status(response)
        payload = self._json(response)
        return ParseResult(
            markdown=str(payload.get("markdown", "")),
            metadata=dict(payload.get("metadata", {})),
        )

    def parse_async(
        self,
        file: FileInput,
        webhook_url: str | None = None,
        *,
        category: str | None = None,
    ) -> JobCreated:
        """Queue a document and return its job identifier."""
        response = self._client.post(
            "/v1/parse/async",
            files=self._multipart_file(file),
            data=self._form_data(category=category, webhook_url=webhook_url),
        )
        self._raise_for_status(response)
        payload = self._json(response)
        return JobCreated(
            job_id=str(payload.get("job_id", "")),
            status=JobStatus(str(payload.get("status", JobStatus.QUEUED.value))),
        )

    def get_job(self, job_id: str | int) -> Job:
        """Fetch one asynchronous job by id."""
        response = self._client.get(f"/v1/jobs/{job_id}")
        self._raise_for_status(response)
        payload = self._json(response)
        result_payload = payload.get("result")
        result = None
        if isinstance(result_payload, dict):
            result = JobResult(
                markdown=str(result_payload.get("markdown", "")),
                metadata=dict(result_payload.get("metadata", {})),
            )
        return Job(
            job_id=str(payload.get("job_id", job_id)),
            status=JobStatus(str(payload.get("status", ""))),
            result=result,
            error=payload.get("error"),
        )

    def wait_for_job(
        self,
        job_id: str | int,
        *,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 1.0,
    ) -> Job:
        """Poll a job until it succeeds or fails, or the timeout expires."""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            job = self.get_job(job_id)
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return job
            if time.monotonic() >= deadline:
                raise JobTimeoutError(
                    f"job {job_id} did not finish within {timeout_seconds:.1f} seconds"
                )
            time.sleep(poll_interval_seconds)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> DocIngestClient:
        """Return the client for context-manager use."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the underlying HTTP client."""
        self.close()

    @staticmethod
    def _multipart_file(file: FileInput) -> dict[str, tuple[str, bytes]]:
        """Convert supported file inputs to a multipart upload mapping."""
        if isinstance(file, bytes):
            return {"file": ("upload.bin", file)}
        path = Path(file)
        return {"file": (path.name, path.read_bytes())}

    @staticmethod
    def _form_data(
        *,
        category: str | None = None,
        webhook_url: str | None = None,
    ) -> dict[str, str]:
        """Build only the optional form fields that are actually set."""
        data: dict[str, str] = {}
        if category is not None:
            data["category"] = category
        if webhook_url is not None:
            data["webhook_url"] = webhook_url
        return data

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Convert HTTP errors into typed SDK errors."""
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                detail_payload = exc.response.json().get("detail", exc.response.text)
            except (json.JSONDecodeError, AttributeError):
                detail_payload = exc.response.text
            raise DocIngestAPIError(
                status_code=exc.response.status_code,
                detail=str(detail_payload),
            ) from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        """Decode a JSON object response."""
        payload = response.json()
        if not isinstance(payload, dict):
            raise DocIngestError("gateway returned a non-object JSON response")
        return payload

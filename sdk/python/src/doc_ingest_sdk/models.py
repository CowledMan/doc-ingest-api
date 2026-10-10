"""Typed response models returned by the Document Ingest SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

FileInput = Path | bytes


class JobStatus(StrEnum):
    """Statuses returned by the gateway job endpoint."""

    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ParseResult:
    """Markdown returned by the synchronous parse endpoint."""

    markdown: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobCreated:
    """Acknowledgement returned after queuing an asynchronous parse."""

    job_id: str
    status: JobStatus


@dataclass(frozen=True)
class JobResult:
    """Markdown and metadata associated with a completed job."""

    markdown: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Job:
    """Current state and optional result of one parse job."""

    job_id: str
    status: JobStatus
    result: JobResult | None = None
    error: str | None = None

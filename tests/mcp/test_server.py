"""Tests for the Document Ingest MCP server."""

from __future__ import annotations

import asyncio
from pathlib import Path

from doc_ingest_sdk import Job, JobResult, JobStatus, ParseResult

from doc_ingest_mcp import create_mcp_server


class FakeGateway:
    """A fake gateway client used to verify MCP tool wiring."""

    def __init__(self) -> None:
        self.parsed_paths: list[Path] = []
        self.checked_jobs: list[str] = []

    def parse(self, file: Path, *, category: str | None = None) -> ParseResult:
        """Return deterministic Markdown and record the call."""
        self.parsed_paths.append(file)
        return ParseResult(
            markdown="# Parsed",
            metadata={"engine_used": "markitdown", "confidence": 0.9},
        )

    def get_job(self, job_id: str | int) -> Job:
        """Return deterministic job state and record the call."""
        self.checked_jobs.append(str(job_id))
        return Job(
            job_id="42",
            status=JobStatus.SUCCEEDED,
            result=JobResult(
                markdown="# Done",
                metadata={"engine_used": "markitdown", "page_count": 2},
            ),
            error=None,
        )


def test_mcp_tools_are_registered() -> None:
    """The MCP server should expose exactly the two gateway-backed tools."""
    server = create_mcp_server(client=FakeGateway())
    tools = asyncio.run(server.list_tools())

    assert [tool.name for tool in tools] == ["parse_document", "check_job"]


def test_parse_document_tool_calls_gateway(tmp_path: Path) -> None:
    """The parse tool should send a local path through the SDK client."""
    gateway = FakeGateway()
    server = create_mcp_server(client=gateway)
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello", encoding="utf-8")

    result = asyncio.run(
        server.call_tool("parse_document", {"file_path": str(file_path)})
    )

    assert result.is_error is False
    assert gateway.parsed_paths == [file_path]
    assert "# Parsed" in result.content[0].text


def test_check_job_tool_calls_gateway() -> None:
    """The job tool should return a structured result from the SDK model."""
    gateway = FakeGateway()
    server = create_mcp_server(client=gateway)

    result = asyncio.run(server.call_tool("check_job", {"job_id": "42"}))

    assert result.is_error is False
    assert gateway.checked_jobs == ["42"]
    assert isinstance(result.structured_content, dict)
    assert result.structured_content["job_id"] == "42"
    assert result.structured_content["status"] == "succeeded"
    assert result.structured_content["markdown"] == "# Done"
    assert result.structured_content["metadata"]["page_count"] == 2

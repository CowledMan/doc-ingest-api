"""Real stdio transport integration tests for the MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

pytestmark = pytest.mark.integration


class _FakeGatewayHandler(BaseHTTPRequestHandler):
    """Minimal HTTP gateway used by the MCP server under test."""

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        """Return a successful parse result for any parse request."""
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        self._send(
            200,
            {
                "markdown": "# Parsed",
                "metadata": {"engine_used": "markitdown", "confidence": 0.9},
            },
        )

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        """Return a successful job result for the expected job id."""
        if self.path == "/v1/jobs/42":
            self._send(
                200,
                {
                    "job_id": 42,
                    "status": "succeeded",
                    "result": {
                        "markdown": "# Done",
                        "metadata": {"page_count": 2},
                    },
                    "error": None,
                },
            )
            return
        self._send(404, {"detail": "job_not_found"})

    def log_message(self, format: str, *args: object) -> None:
        """Suppress noisy request logging."""
        del format, args


@pytest.fixture
def fake_gateway() -> Iterator[str]:
    """Start an ephemeral fake gateway and return its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_stdio_mcp_handshake_and_tool_calls(
    tmp_path: Path,
    fake_gateway: str,
) -> None:
    """Exercise initialize, tools/list, and call_tool over real stdio transport."""
    venv_python = Path.cwd() / ".venv" / "Scripts" / "python.exe"
    command = str(venv_python if venv_python.is_file() else sys.executable)
    params = StdioServerParameters(
        command=command,
        args=["-m", "doc_ingest_mcp"],
        cwd=Path.cwd(),
        env={
            **os.environ,
            "DOC_INGEST_BASE_URL": fake_gateway,
            "DOC_INGEST_API_KEY": "test-key",
        },
    )

    sample = tmp_path / "sample.txt"
    sample.write_text("hello", encoding="utf-8")

    async def run_client() -> tuple[list[str], str, Any]:
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialization = await session.initialize()
            tools = await session.list_tools()
            parse_result = await session.call_tool(
                "parse_document",
                {"file_path": str(sample)},
            )
            job_result = await session.call_tool(
                "check_job",
                {"job_id": "42"},
            )
            return (
                [tool.name for tool in tools.tools],
                initialization.server_info.name,
                (parse_result, job_result),
            )

    tool_names, server_name, (parse_result, job_result) = asyncio.run(run_client())

    assert server_name == "doc-ingest"
    assert tool_names == ["parse_document", "check_job"]
    assert parse_result.is_error is False
    assert parse_result.content[0].text == "# Parsed"
    assert job_result.is_error is False
    assert job_result.structured_content["status"] == "succeeded"
    assert job_result.structured_content["markdown"] == "# Done"

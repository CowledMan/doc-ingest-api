"""Model Context Protocol server for the Document Ingest gateway."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from doc_ingest_sdk import DocIngestClient
from mcp.server.mcpserver import MCPServer


def _client_from_environment() -> DocIngestClient:
    """Create a gateway client from MCP runtime environment variables."""
    base_url = os.environ.get("DOC_INGEST_BASE_URL", "http://localhost:8000")
    api_key = os.environ.get("DOC_INGEST_API_KEY")
    if not api_key:
        raise ValueError("DOC_INGEST_API_KEY is required")
    return DocIngestClient(base_url=base_url, api_key=api_key)


def create_mcp_server(client: DocIngestClient | None = None) -> MCPServer:
    """Create the stdio MCP server backed by the Document Ingest gateway."""
    active_client = client or _client_from_environment()
    server = MCPServer(
        name="doc-ingest",
        title="Document Ingest",
        version="0.1.0",
        instructions="Parse local documents and inspect asynchronous parse jobs.",
    )

    @server.tool()
    def parse_document(file_path: str) -> str:
        """Parse a local document through the gateway and return Markdown."""
        return active_client.parse(Path(file_path)).markdown

    @server.tool()
    def check_job(job_id: str) -> dict[str, Any]:
        """Check the current status and result of one parse job."""
        job = active_client.get_job(job_id)
        return {
            "job_id": job.job_id,
            "status": job.status.value,
            "markdown": job.result.markdown if job.result else None,
            "metadata": job.result.metadata if job.result else None,
            "error": job.error,
        }

    return server


def main() -> None:
    """Run the MCP server on stdio."""
    create_mcp_server().run("stdio")

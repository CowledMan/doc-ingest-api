"""Smoke-test the MCP stdio transport with a live gateway."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BASE = "http://127.0.0.1:8401"
KEY = os.environ.get("DOC_INGEST_API_KEY", "")
ROOT = Path.cwd()


async def run(label: str, params: StdioServerParameters) -> None:
    """Connect over stdio, list tools, and parse one sample document."""
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await asyncio.wait_for(session.initialize(), timeout=20)
        tools = await asyncio.wait_for(session.list_tools(), timeout=10)
        names = sorted(t.name for t in tools.tools)
        assert "parse_document" in names and "check_job" in names, names
        sample = next(ROOT.glob("benchmarks/dataset/**/*.pdf"), None) or next(
            ROOT.glob("benchmarks/dataset/**/*.*")
        )
        result = await asyncio.wait_for(
            session.call_tool("parse_document", {"file_path": str(sample)}),
            timeout=120,
        )
        text = "".join(c.text for c in result.content if hasattr(c, "text"))
        assert len(text) > 500, f"markdown too short: {len(text)}"
        print(
            f"[{label}] OK server={init.server_info.name} tools={names} "
            f"file={sample.name} markdown_chars={len(text)}"
        )


async def main() -> None:
    """Run both MCP entrypoint paths."""
    env: dict[str, str] = {
        **os.environ,
        "DOC_INGEST_BASE_URL": BASE,
        "DOC_INGEST_API_KEY": KEY,
    }
    await run(
        "uv doc-ingest-mcp",
        StdioServerParameters(
            command="uv", args=["run", "doc-ingest-mcp"], cwd=str(ROOT), env=env
        ),
    )
    await run(
        "python -m doc_ingest_mcp",
        StdioServerParameters(
            command=sys.executable, args=["-m", "doc_ingest_mcp"], cwd=str(ROOT), env=env
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())

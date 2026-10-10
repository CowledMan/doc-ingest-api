# MCP Server

The Document Ingest API exposes a [Model Context Protocol](https://modelcontextprotocol.io/) server so AI agents can parse documents.

## Running

```bash
# Recommended
uv run doc-ingest-mcp

# Or via module
uv run python -m doc_ingest_mcp
```

## Environment

| Variable | Default | Description |
|---|---|---|
| `DOC_INGEST_BASE_URL` | `http://localhost:8000` | API gateway URL |
| `DOC_INGEST_API_KEY` | (required) | API key |

## Tools

### `parse_document`

Parse a local file and return Markdown.

```json
{"file_path": "/path/to/document.pdf"}
```

### `check_job`

Check the status of an async parse job.

```json
{"job_id": 42}
```

## Claude Desktop config

```json
{
  "mcpServers": {
    "doc-ingest": {
      "command": "uv",
      "args": ["run", "doc-ingest-mcp"],
      "cwd": "/path/to/01_文档摄取API",
      "env": {
        "DOC_INGEST_BASE_URL": "http://localhost:8000",
        "DOC_INGEST_API_KEY": "dip_..."
      }
    }
  }
}
```

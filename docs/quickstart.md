# Quickstart

Get up and running in under 5 minutes.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## 1. Install

```bash
uv sync
```

## 2. Create an API key

```bash
uv run python -m doc_ingest_api.manage_keys create --name demo
# Output: dip_your_key_here
```

## 3. Start the server

```bash
uv run uvicorn doc_ingest_api.asgi:app --reload
# INFO:     Uvicorn running on http://127.0.0.1:8000
```

## 4. Parse a document

### Synchronous

```bash
curl -X POST http://localhost:8000/v1/parse \
  -H "X-API-Key: dip_your_key_here" \
  -F "file=@report.pdf" \
  -F "category=financial_reports"
```

### Asynchronous

```bash
curl -X POST http://localhost:8000/v1/parse/async \
  -H "X-API-Key: dip_your_key_here" \
  -F "file=@report.pdf"
# → {"job_id": 1, "status": "queued"}

curl http://localhost:8000/v1/jobs/1 \
  -H "X-API-Key: dip_your_key_here"
```

## 5. Check billing (mock)

```bash
curl http://localhost:8000/v1/billing/subscription \
  -H "X-API-Key: dip_your_key_here"
# → {"billing_provider": "mock", "status": "active", ...}
```

## Next steps

- Explore the [API reference](api.md)
- Use the [Python SDK](sdk.md) for typed calls
- Connect AI agents via the [MCP server](mcp.md)
- Deploy with [Docker](deploy.md)

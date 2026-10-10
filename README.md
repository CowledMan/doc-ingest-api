# Document Ingest API

Multi-engine document parsing API with confidence-based routing between **anydoc**, **markitdown**, and **docling**.

## Architecture

```
┌─────────────────────────────────────────────────┐
│            Client (SDK / MCP / curl)             │
└────────────────────┬────────────────────────────┘
                     │ HTTPS (X-API-Key)
                     ▼
┌─────────────────────────────────────────────────┐
│             FastAPI Gateway (:8000)              │
│    auth → quota → category → parser routing     │
│                                                 │
│  POST /v1/parse            (sync)               │
│  POST /v1/parse/async      (queue → job)        │
│  GET  /v1/jobs/{id}                             │
│  POST /v1/billing/checkout     (mock)           │
│  POST /v1/billing/webhook      (mock)           │
│  GET  /v1/billing/subscription (mock)           │
│  GET  /healthz                                  │
└──────┬──────────────────────────────┬───────────┘
       │ background / celery          │
       ▼                              ▼
┌───────────────┐          ┌──────────────────┐
│    Parser     │          │  Billing (mock)  │
│    Router     │          │  Stripe M2       │
│ anydoc (MIT)  │          └──────────────────┘
│ markitdown    │
│ docling       │
└───────────────┘
```

## Quickstart

```bash
# 1. Install
uv sync

# 2. Create an API key
uv run python -m doc_ingest_api.manage_keys create --name demo
# → dip_your_key_here

# 3. Start the server
uv run uvicorn doc_ingest_api.asgi:app --reload

# 4. Parse a document
curl -X POST http://localhost:8000/v1/parse \
  -H "X-API-Key: dip_your_key_here" \
  -F "file=@sample.pdf"
```

## Docker

```bash
cp .env.example .env
docker compose up -d
curl http://localhost:8000/healthz
```

## SDK

```python
from doc_ingest_sdk import DocIngestClient

client = DocIngestClient(base_url="http://localhost:8000", api_key="dip_...")
result = client.parse_document(file_path="report.pdf", category="financial_reports")
print(result.markdown)
```

## MCP Server (AI agents)

```bash
uv run doc-ingest-mcp
# or
uv run python -m doc_ingest_mcp
```

## Pricing & Limits

| Plan | Monthly Pages | Price |
|------|--------------|-------|
| Starter | 500 | $0 (mock) |
| Pro | 5,000 | TBD (M2) |
| Scale | 50,000 | TBD (M2) |

> **Billing is mocked in M1.** All billing responses include `billing_provider: "mock"`. Real Stripe integration is planned for M2 — no code changes needed to switch.

## Documentation

Full documentation is in `docs/`. Build locally:

```bash
uv run mkdocs build
# → site/
```

Or serve live:

```bash
uv run mkdocs serve
```

## Benchmarks

See `benchmarks/dataset/README.md` for the 8-category × 25-document evaluation framework.

## License

All dependencies are MIT or permissively licensed. No AGPL dependencies are permitted (see `AGENTS.md`).

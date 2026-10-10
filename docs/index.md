# Document Ingest API

Multi-engine document parsing API that routes between **anydoc**, **markitdown**, and **docling** based on document category and confidence scores.

## Architecture

```
┌──────────────────────────────────────────────┐
│                Client (SDK / MCP)             │
└──────────────────┬───────────────────────────┘
                   │ HTTPS (X-API-Key)
                   ▼
┌──────────────────────────────────────────────┐
│           FastAPI Gateway (:8000)             │
│  auth → quota → category → parser routing    │
│                                              │
│  POST /v1/parse          (sync)              │
│  POST /v1/parse/async    (queue → job)       │
│  GET  /v1/jobs/{id}                          │
│  POST /v1/billing/checkout   (mock)          │
│  POST /v1/billing/webhook    (mock)          │
│  GET  /v1/billing/subscription (mock)        │
│  GET  /healthz                               │
└──────┬───────────────────────────┬───────────┘
       │ background / celery       │
       ▼                           ▼
┌──────────────┐        ┌─────────────────┐
│   Parser     │        │  Billing (mock) │
│   Router     │        │  Stripe M2      │
│ anydoc       │        └─────────────────┘
│ markitdown   │
│ docling      │
└──────────────┘
```

## 5-Minute Quickstart

```bash
uv sync
uv run python -m doc_ingest_api.manage_keys create --name demo
uv run uvicorn doc_ingest_api.asgi:app --reload

# In another terminal:
curl -X POST http://localhost:8000/v1/parse \
  -H "X-API-Key: dip_..." \
  -F "file=@sample.pdf"
```

## Docker One-Liner

```bash
docker compose up -d
curl http://localhost:8000/healthz
```

## Pricing (Mock Phase)

| Plan | Monthly Pages | Price |
|------|--------------|-------|
| Starter | 500 | $0 (mock) |
| Pro | 5,000 | TBD (M2) |
| Scale | 50,000 | TBD (M2) |

> **Note:** Billing is fully mocked in M1. Responses include `billing_provider: "mock"` so you can integrate now and switch later without code changes.

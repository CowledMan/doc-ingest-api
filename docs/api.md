# API Reference

Base URL: `http://localhost:8000` (dev)

Authentication: `X-API-Key` header (see [Quickstart](quickstart.md#2-create-an-api-key)).

## Endpoints

### `POST /v1/parse`

Parse a document synchronously and return Markdown.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `file` | multipart | Yes | Document file (PDF, DOCX, XLSX, PPTX, …) |
| `category` | string | No | Hint for parser routing: `contracts`, `financial_reports`, `scanned_documents`, `presentations`, `spreadsheets`, `academic_papers`, `official_red_header`, `formula_heavy` |

**Response:**

```json
{
  "markdown": "# Parsed content",
  "metadata": {
    "engine_used": "markitdown",
    "confidence": 0.9,
    "page_count": 3,
    "duration_ms": 1420.5
  }
}
```

### `POST /v1/parse/async`

Queue a document for background parsing. Returns `202 Accepted`.

Same parameters as `/v1/parse`, plus:

| Parameter | Type | Required | Description |
|---|---|---|---|
| `webhook_url` | string | No | URL to POST the result when done |

**Response:**

```json
{"job_id": 1, "status": "queued"}
```

### `GET /v1/jobs/{job_id}`

Poll an async parse job.

**Response:**

```json
{
  "job_id": 1,
  "status": "succeeded",
  "result": {"markdown": "# Done", "metadata": {"engine_used": "docling"}},
  "error": null
}
```

Statuses: `queued` → `processing` → `succeeded` | `failed`

### `POST /v1/billing/checkout`

Create a mock checkout session. Returns a Stripe-shaped response with `billing_provider: "mock"`.

**Response:**

```json
{
  "billing_provider": "mock",
  "checkout_session_id": "cs_test_abc123",
  "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_abc123",
  "customer_id": "cus_test_00000001",
  "price_id": "price_test_starter"
}
```

### `POST /v1/billing/webhook`

Verify webhook signature. Requires `Stripe-Signature` header.

**Response:**

```json
{
  "billing_provider": "mock",
  "verified": true,
  "event_type": "checkout.session.completed"
}
```

### `GET /v1/billing/subscription`

Return mock subscription status.

**Response:**

```json
{
  "billing_provider": "mock",
  "subscription_id": "sub_test_00000001",
  "status": "active",
  "plan_name": "starter",
  "monthly_page_quota": 500
}
```

### `GET /healthz`

Liveness probe. Returns `{"status": "ok"}`.

## Error Codes

| HTTP | Code | Description |
|---|---|---|
| 400 | `missing_signature` | Webhook `Stripe-Signature` header absent |
| 401 | `missing_api_key` | No `X-API-Key` header |
| 401 | `invalid_api_key` | Key not found or inactive |
| 404 | `job_not_found` | Job does not exist or belongs to another key |
| 429 | `quota_exceeded` | Monthly page quota exhausted |
| 502 | `parser_failed` | All parsers failed |
| 503 | `ocr_not_configured` | Scanned document but no OCR key |
| 504 | `parser_timeout` | Parser exceeded timeout |

## Quotas

Each API key has a rolling monthly page quota (default 500). Pages are counted from parser metadata `page_count`. Quota resets on the first day of each UTC month.

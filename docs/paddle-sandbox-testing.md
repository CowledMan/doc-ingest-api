# Paddle Sandbox Testing Guide

## Prerequisites

1. Paddle sandbox account ([https://sandbox-vendors.paddle.com](https://sandbox-vendors.paddle.com))
2. Sandbox API key (Developer Tools → Authentication → API Keys)
3. Client-side token (Developer Tools → Authentication → Client-side tokens, `test_` prefix)

## Setup

### 1. Seed the product catalog

```bash
export PADDLE_SANDBOX_API_KEY=pdl_sdbx_...
uv run python scripts/setup_paddle_catalog.py
```

Output includes `PADDLE_PRICE_STARTER` and `PADDLE_PRICE_PRO` price IDs.

### 2. Configure environment variables

```bash
export PADDLE_ENV=sandbox
export PADDLE_API_KEY=pdl_sdbx_...
export PADDLE_WEBHOOK_SECRET=pdl_ntfset_...
export PADDLE_CLIENT_TOKEN=test_...
export PADDLE_PRICE_STARTER=pri_...
export PADDLE_PRICE_PRO=pri_...
```

### 3. Set up webhook notification destination

In the sandbox dashboard:

1. Go to **Developer Tools → Notifications**
2. Click **New destination**
3. URL: `https://tomdocs.com/v1/billing/webhook` (or your local tunnel URL)
4. Events: `subscription.created`, `subscription.updated`, `subscription.canceled`
5. Copy the generated **secret** → set as `PADDLE_WEBHOOK_SECRET`

For local testing, use a tunnel (ngrok / Cloudflare Tunnel):
```bash
ngrok http 8000
# Use the ngrok URL as the notification destination URL
```

## End-to-End Test

### 1. Start the server

```bash
uv run uvicorn doc_ingest_api.asgi:app --reload
```

### 2. Open the landing page

Navigate to `http://localhost:8000/`. Verify:

- Pricing table shows Starter $19 and Pro $99
- Paddle.js script is loaded
- "Get Starter" and "Get Pro" buttons are visible

### 3. Create a demo key

```bash
curl -X POST http://localhost:8000/v1/demo-keys
```

### 4. Click "Get Starter" or "Get Pro"

The Paddle checkout overlay should open. Use the Paddle sandbox test card:

| Field | Value |
|-------|-------|
| Card number | `4242 4242 4242 4242` |
| Expiry | Any future date |
| CVC | Any 3 digits |

### 5. Verify webhook delivery

After checkout completes:

```bash
# Check server logs for webhook event processing
# Or query the subscription endpoint
curl http://localhost:8000/v1/billing/subscription \
  -H "X-API-Key: dip_..."
```

The response should show `plan_name: "starter"` or `"pro"` and the updated quota.

### 6. Verify quota update

```bash
# Check remaining quota via the parse endpoint
curl -X POST http://localhost:8000/v1/parse \
  -H "X-API-Key: dip_..." \
  -F "file=@assets/samples/demo.pdf"
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Webhook returns 401 | `PADDLE_WEBHOOK_SECRET` mismatch; copy from notification settings |
| Checkout button does nothing | `PADDLE_CLIENT_TOKEN` not set or `PADDLE_ENV` not `sandbox` |
| Config endpoint returns mock | `PADDLE_ENV` not set on server |
| Plan not updated after checkout | Check webhook delivery in Paddle dashboard → Notifications → Logs |

## License Note

The Paddle Python SDK (`paddle-python`) is Apache-2.0 licensed. We use direct
REST API calls via `httpx` (already a project dependency) to avoid adding a
new dependency. No AGPL dependencies are introduced.

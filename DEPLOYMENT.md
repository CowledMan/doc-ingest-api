# Deployment Guide

## Render (current)

The API is deployed on Render at `https://doc-ingest-api.onrender.com`.

### Environment Variables

| Variable | Value | Description |
|----------|-------|-------------|
| `DATABASE_URL` | `sqlite:////app/data/doc_ingest.db` | SQLite volume (default) or `postgresql://...` |
| `QUEUE_BACKEND` | `background` | In-process task queue (no Redis needed) |
| `PADDLE_ENV` | `sandbox` or `live` | Empty = mock billing |
| `PADDLE_API_KEY` | `pdl_...` | Paddle API key (server-side) |
| `PADDLE_WEBHOOK_SECRET` | `pdl_ntfset_...` | Webhook signing secret |
| `PADDLE_CLIENT_TOKEN` | `test_...` / `live_...` | Paddle.js token (public) |
| `PADDLE_PRICE_STARTER` | `pri_...` | Starter price ID |
| `PADDLE_PRICE_PRO` | `pri_...` | Pro price ID |
| `ALIYUN_OCR_KEY` | (empty) | OCR key (stub in M1) |
| `RATE_LIMIT_IP_PER_HOUR` | `50` | IP-level rate limit |
| `RATE_LIMIT_KEY_PER_MINUTE` | `60` | API-key-level rate limit |

### Free Tier Disk Reset Risk

**Render free tier disks are ephemeral.** On every deploy or restart:
- SQLite data is **lost**
- Uploaded files are **lost**
- API keys are **lost**

**Mitigation options:**

| Option | Cost | Steps |
|--------|------|-------|
| Render Paid Disk ($0.25/GB/mo) | Paid | Dashboard → Disk → Add mount at `/app/data` |
| External Postgres (Neon free tier) | Free | Set `DATABASE_URL` to Neon connection string |
| External Postgres (Supabase free tier) | Free | Same as above |

## Switching to Postgres (Neon)

### 1. Create a Neon account

Go to [https://neon.tech](https://neon.tech) and create a free project.

### 2. Copy the connection string

From the Neon dashboard → Connection Details → copy the `postgresql://...` URI.

### 3. Update Render environment

In the Render dashboard → Environment → set:

```
DATABASE_URL=postgresql://user:pass@host.region.aws.neon.tech/dbname?sslmode=require
```

### 4. Deploy

The SQLAlchemy models are dialect-agnostic (no SQLite-specific code).
`Base.metadata.create_all()` runs on startup and creates the tables.

No code changes are needed. The rate limiter and task queue are in-memory and
unaffected by the database switch.

## Fly.io

See `fly.toml` and [docs/deploy.md](docs/deploy.md) for Fly.io configuration.

## Railway

See `railway.json` and [docs/deploy.md](docs/deploy.md) for Railway configuration.
## Customer Portal (Subscription Management)

Paying users can manage their subscription (cancel, update card, view invoices) via the Paddle customer portal.

### API Usage

```bash
# Open the subscription management portal (requires API key with active subscription)
curl -X POST https://tomdocs.com/v1/billing/portal \
  -H "X-API-Key: dip_..."
```

Response (success):

```json
{"billing_provider": "paddle", "portal_url": "https://sandbox-portal.paddle.com/portal-session/..."}
```

Response (no subscription):

```json
{"detail": {"code": "no_subscription", "message": "No active subscription found; subscribe first"}}
```

### Flow

1. User subscribes via Paddle Checkout (landing page → Get Starter/Pro)
2. Paddle sends `subscription.created` webhook → server stores `paddle_customer_id` on the ApiKey row
3. User calls `POST /v1/billing/portal` → server creates a Paddle portal session using the stored customer ID
4. User is redirected to Paddle's hosted portal (cancel / update card / invoices)
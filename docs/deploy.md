# Deployment

## Docker Compose (recommended)

```bash
cp .env.example .env
# Edit .env: set DOC_INGEST_API_KEY, DATABASE_URL, etc.
docker compose up -d
```

Services:

| Service | Port | Description |
|---|---|---|
| `api` | 8000 | FastAPI gateway |
| `redis` | 6379 | Celery broker (when `QUEUE_BACKEND=celery`) |

### Health check

```bash
curl http://localhost:8000/healthz
```

### Logs

```bash
docker compose logs -f api
```

## Dockerfile

The Dockerfile uses a multi-stage build:

1. **Builder stage** — `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` installs dependencies via `uv sync --frozen`.
2. **Runtime stage** — `python:3.12-slim` copies only the venv and source; runs as non-root `appuser`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./doc_ingest.db` | SQLAlchemy connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker URL |
| `QUEUE_BACKEND` | `background` | `background` (in-process) or `celery` |
| `DOC_INGEST_API_KEY` | (none) | Default key for Docker deployment |
| `STRIPE_API_KEY` | (empty) | Empty → mock billing provider |
| `STRIPE_WEBHOOK_SECRET` | (empty) | Webhook signing secret |

## Production notes

- Use PostgreSQL instead of SQLite (`DATABASE_URL=postgresql://...`)
- Set `QUEUE_BACKEND=celery` and scale the Redis broker separately
- Configure TLS termination (nginx / Caddy / cloud LB)
- Set real Stripe keys only when migrating from mock to real billing

## Fly.io

```bash
# Install flyctl (one-time)
# macOS: brew install flyctl
# Linux: curl -L https://fly.io/install.sh | sh

# Login and launch
flyctl auth login
flyctl launch --no-deploy  # creates fly.toml (already committed)

# Create a persistent volume for SQLite
flyctl volumes create doc_ingest_data --size 1 --region sin

# Set secrets (optional)
flyctl secrets set DOC_INGEST_API_KEY=dip_... ALIYUN_OCR_KEY=...

# Deploy
flyctl deploy

# Verify
curl https://doc-ingest-api.fly.dev/healthz
```

### SQLite persistence on Fly

The `fly.toml` mounts a volume at `/data` and sets `DATABASE_URL=sqlite:////data/doc_ingest.db`.
SQLite works well for single-instance, low-to-moderate traffic.

**When to switch to Postgres:**

| Trigger | Action |
|---------|--------|
| Traffic > ~100 req/min sustained | Switch to Postgres |
| Need multi-region or multi-instance | Switch to Postgres |
| Need Celery workers with shared state | Switch to Postgres |
| Simple API + background queue | SQLite is fine |

To switch: `flyctl postgres create` → set `DATABASE_URL` to the Postgres URL → deploy.

## Railway

```bash
# Install railway CLI (one-time)
npm install -g @railway/cli

# Login and init
railway login
railway init  # select existing project or create new

# Add environment variables
railway variables set DOC_INGEST_API_KEY=dip_... QUEUE_BACKEND=background

# Deploy
railway up

# Verify
railway status
curl https://<railway-domain>/healthz
```

Railway reads `railway.json` for build/deploy config. The `preDeployCommand`
ensures the database schema exists on each deploy. For persistence, add a
Railway Volume plugin and set `DATABASE_URL=sqlite:////data/doc_ingest.db`.

## Launch checklist

See [launch-checklist.md](launch-checklist.md) for the M2→M3 go-live checklist.
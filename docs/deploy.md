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

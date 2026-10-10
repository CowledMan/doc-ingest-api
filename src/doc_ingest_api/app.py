"""FastAPI gateway for document parsing."""

from __future__ import annotations

import asyncio
import html
import json
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session, sessionmaker

from .billing import create_billing_provider
from .config import Settings
from .models import ApiKey, ParseJob
from .parsers import (
    OcrNotConfiguredError,
    ParserError,
    ParseResult,
    ParserRouter,
    ParserTimeoutError,
    create_parser_router,
)
from .quotas import ensure_monthly_period, record_page_usage
from .rate_limit import InMemoryRateLimiter, RateLimiter
from .security import find_api_key, generate_api_key, hash_api_key
from .tasks import build_task_queue


@dataclass(frozen=True)
class _AppState:
    """Objects shared with request dependencies."""

    settings: Settings
    session_factory: sessionmaker[Session]
    parser_router: ParserRouter
    billing_provider: Any
    rate_limiter: RateLimiter


def _app_state(request: Request) -> _AppState:
    """Read typed application state attached by ``create_app``."""
    return cast(_AppState, request.app.state.doc_ingest)


def _session_factory(request: Request) -> sessionmaker[Session]:
    """Return the session factory configured for the current app."""
    return _app_state(request).session_factory


def _get_db(
    session_factory: Annotated[sessionmaker[Session], Depends(_session_factory)],
) -> Iterator[Session]:
    """Yield a request-scoped database session."""
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _get_api_key(
    session: Annotated[Session, Depends(_get_db)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ApiKey:
    """Authenticate the request and reject keys that have exhausted quota."""
    if not x_api_key:
        raise _api_error(
            401,
            "missing_api_key",
            "Set the X-API-Key header",
        )
    api_key = find_api_key(session, x_api_key)
    if api_key is None or not api_key.active:
        raise _api_error(401, "invalid_api_key", "API key is invalid or inactive")
    ensure_monthly_period(api_key)
    if api_key.used_pages >= api_key.monthly_page_quota:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "quota_exceeded",
                "message": "Monthly page quota exceeded",
                "monthly_page_quota": api_key.monthly_page_quota,
            },
        )
    return api_key


async def _save_upload_to_temp(file: UploadFile) -> Path:
    """Persist an uploaded file to a temporary path for parser subprocesses."""
    suffix = Path(file.filename or "upload").suffix
    content = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
        handle.write(content)
        return Path(handle.name)


def _response_metadata(result: ParseResult, duration_ms: float) -> dict[str, Any]:
    """Build the metadata returned by the synchronous parse endpoint."""
    metadata = dict(result.metadata)
    metadata.update(
        {
            "engine_used": result.engine_used,
            "duration_ms": duration_ms,
            "confidence": result.confidence,
        }
    )
    metadata.setdefault("page_count", 1)
    return metadata


def _page_count(metadata: Mapping[str, Any]) -> int:
    """Return a positive page count from parser metadata."""
    try:
        return max(1, int(metadata.get("page_count", 1)))
    except (TypeError, ValueError):
        return 1


def _decode_metadata(raw_metadata: str | None) -> dict[str, Any]:
    """Decode stored job metadata, treating malformed data as empty."""
    if not raw_metadata:
        return {}
    try:
        value = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _api_error(status_code: int, code: str, message: str) -> HTTPException:
    """Create a consistent machine-readable API error."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def create_app(
    settings: Settings,
    *,
    session_factory: sessionmaker[Session] | None = None,
    parser_router: ParserRouter | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    """Create the FastAPI application with configured storage and parsers."""
    if session_factory is None:
        from .database import create_database

        database = create_database(settings.database_url)
        database.create_all()
        session_factory = database.session_factory
    if parser_router is None:
        parser_router = create_parser_router(settings)
    billing_provider = create_billing_provider(
        stripe_secret_key=settings.stripe_api_key,
        stripe_webhook_secret=settings.stripe_webhook_secret,
    )
    if rate_limiter is None:
        rate_limiter = InMemoryRateLimiter()

    app = FastAPI(
    title="Document Ingest API",
    version="0.2.0",
    description=(
        "Multi-engine document parsing API with confidence-based routing "
        "between anydoc, markitdown, and docling."
    ),
    contact={"name": "Document Ingest", "url": "https://github.com/user/doc-ingest-api"},
    license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
)
    app.state.doc_ingest = _AppState(
        settings=settings,
        session_factory=session_factory,
        parser_router=parser_router,
        billing_provider=billing_provider,
        rate_limiter=rate_limiter,
    )

    # --- Rate limiting middleware ---
    @app.middleware("http")
    async def rate_limit_middleware(
        request: Request,
        call_next: Any,
    ) -> Any:
        """Apply IP/key-based rate limiting based on authentication state."""
        state = _app_state(request)
        x_api_key = request.headers.get("X-API-Key")
        if x_api_key:
            key_id = f"key:{hash_api_key(x_api_key)[:16]}"
            result = state.rate_limiter.check(
                key_id,
                limit=state.settings.rate_limit_key_per_minute,
                window_seconds=60,
            )
        else:
            client_ip = request.client.host if request.client else "unknown"
            result = state.rate_limiter.check(
                f"ip:{client_ip}",
                limit=state.settings.rate_limit_ip_per_hour,
                window_seconds=3600,
            )
        if not result.allowed:
            return HTMLResponse(
                content='{"detail":{"code":"rate_limited","message":"Too many requests"}}',
                status_code=429,
                headers={
                    "Retry-After": str(result.retry_after_seconds),
                    "Content-Type": "application/json",
                },
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        return response
    router = app.router

    @router.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        """Return a lightweight liveness response."""
        return {"status": "ok"}

    @router.post("/v1/parse", tags=["parse"])
    async def parse_document(
        request: Request,
        api_key: Annotated[ApiKey, Depends(_get_api_key)],
        file: UploadFile,
        category: str | None = Form(default=None),
    ) -> dict[str, Any]:
        """Parse one uploaded document synchronously."""
        state = _app_state(request)
        temp_path = await _save_upload_to_temp(file)
        started = time.perf_counter()
        try:
            result = await asyncio.to_thread(state.parser_router.parse, temp_path, category)
        except OcrNotConfiguredError as exc:
            raise _api_error(503, "ocr_not_configured", str(exc)) from exc
        except ParserTimeoutError as exc:
            raise _api_error(504, "parser_timeout", str(exc)) from exc
        except ParserError as exc:
            raise _api_error(502, "parser_failed", str(exc)) from exc
        finally:
            temp_path.unlink(missing_ok=True)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        metadata = _response_metadata(result, duration_ms)
        record_page_usage(
            state.session_factory,
            api_key_id=api_key.id,
            pages=_page_count(metadata),
        )
        response: dict[str, Any] = {"markdown": result.markdown, "metadata": metadata}
        if api_key.plan == "demo":
            response["upgrade_url"] = "/#pricing"
        return response

    @router.post("/v1/parse/async", status_code=202, tags=["parse"])
    async def queue_parse_document(
        request: Request,
        api_key: Annotated[ApiKey, Depends(_get_api_key)],
        background_tasks: BackgroundTasks,
        file: UploadFile,
        category: str | None = Form(default=None),
        webhook_url: str | None = Form(default=None),
    ) -> dict[str, Any]:
        """Queue one uploaded document and return a job identifier."""
        state = _app_state(request)
        content = await file.read()
        file_name = file.filename or "upload"
        with state.session_factory.begin() as session:
            job = ParseJob(
                api_key_id=api_key.id,
                status="queued",
                file_name=file_name,
                category=category,
                content=content,
                webhook_url=webhook_url or state.settings.webhook_url,
            )
            session.add(job)
            session.flush()
            job_id = job.id

        queue = build_task_queue(
            state.settings,
            background_tasks=background_tasks,
            session_factory=state.session_factory,
            parser_router=state.parser_router,
        )
        queue.enqueue_parse_job(job_id)
        return {"job_id": job_id, "status": "queued"}

    @router.get("/v1/jobs/{job_id}", tags=["jobs"])
    def get_parse_job(
        job_id: int,
        api_key: Annotated[ApiKey, Depends(_get_api_key)],
        session: Annotated[Session, Depends(_get_db)],
    ) -> dict[str, Any]:
        """Return the current status and result for one parse job."""
        job = session.get(ParseJob, job_id)
        if job is None or job.api_key_id != api_key.id:
            raise _api_error(404, "job_not_found", "Parse job not found")
        result = None
        if job.status == "succeeded":
            result = {
                "markdown": job.markdown or "",
                "metadata": _decode_metadata(job.result_metadata),
            }
        return {
            "job_id": job.id,
            "status": job.status,
            "result": result,
            "error": job.error,
        }


    @router.post("/v1/billing/checkout", tags=["billing"])
    def create_checkout(
        request: Request,
        api_key: Annotated[ApiKey, Depends(_get_api_key)],
    ) -> dict[str, Any]:
        """Create a mock checkout session (real Stripe in M2)."""
        state = _app_state(request)
        session_result = state.billing_provider.create_checkout_session(
            customer_id=f"cus_test_{api_key.id:08d}",
            price_id="price_test_starter",
            success_url="https://example.test/success",
            cancel_url="https://example.test/cancel",
        )
        return {
            "billing_provider": session_result.provider,
            "checkout_session_id": session_result.id,
            "checkout_url": session_result.url,
            "customer_id": session_result.customer_id,
            "price_id": session_result.price_id,
        }

    @router.post("/v1/billing/webhook", tags=["billing"])
    def billing_webhook(
        request: Request,
    ) -> dict[str, Any]:
        """Verify webhook signature header and acknowledge (mock mode)."""
        state = _app_state(request)
        signature = request.headers.get("stripe-signature", "")
        if not signature:
            raise _api_error(400, "missing_signature", "Stripe-Signature header is required")
        body = b""
        verification = state.billing_provider.verify_webhook_signature(
            payload=body,
            signature_header=signature,
            secret=state.settings.stripe_webhook_secret,
        )
        return {
            "billing_provider": verification.provider,
            "verified": verification.verified,
            "event_type": verification.event_type,
        }

    @router.get("/v1/billing/subscription", tags=["billing"])
    def get_subscription(
        request: Request,
        api_key: Annotated[ApiKey, Depends(_get_api_key)],
    ) -> dict[str, Any]:
        """Return mock subscription status and plan details."""
        state = _app_state(request)
        subscription = state.billing_provider.get_subscription(
            f"sub_test_{api_key.id:08d}"
        )
        return {
            "billing_provider": subscription.provider,
            "subscription_id": subscription.id,
            "status": subscription.status,
            "plan_name": subscription.plan_name,
            "monthly_page_quota": subscription.monthly_page_quota,
        }

    # --- Landing page (self-contained, no frontend framework) ---

    def _render_pricing_table() -> str:
        """Build pricing table rows from settings JSON."""
        import json as _json

        try:
            plans = _json.loads(settings.pricing_plans_json)
        except _json.JSONDecodeError:
            plans = []
        rows: list[str] = []
        for plan in plans:
            name = html.escape(str(plan.get("name", "")))
            pages = html.escape(str(plan.get("monthly_pages", "")))
            price = html.escape(str(plan.get("price_usd", "")))
            rows.append(
                f"<tr><td>{name}</td><td>{pages} pages/mo</td>"
                f'<td>${price}/mo</td></tr>'
            )
        return "\n".join(rows)

    @router.get("/", tags=["health"], response_class=HTMLResponse, include_in_schema=False)
    def landing() -> HTMLResponse:
        """Serve a self-contained landing page with pricing."""
        pricing = _render_pricing_table()
        return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Document Ingest API</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;padding:2rem;background:#f8f9fa;color:#1a1a2e}}
h1{{font-size:2.2rem;margin-bottom:.5rem}} h2{{margin-top:2rem}}
.tagline{{font-size:1.2rem;color:#555;margin-bottom:2rem}}
pre{{background:#1a1a2e;color:#a8dadc;padding:1rem;border-radius:8px;overflow-x:auto;font-size:.85rem}}
code{{font-family:monospace}}
table{{border-collapse:collapse;margin:1rem 0;width:100%}}
th,td{{border:1px solid #ddd;padding:.6rem 1rem;text-align:left}}
th{{background:#e9ecef}}
.note{{font-size:.85rem;color:#888}}
a{{color:#457b9d}}
.badge{{display:inline-block;background:#457b9d;color:#fff;padding:.2rem .6rem;border-radius:4px;font-size:.8rem;margin-right:.5rem}}
</style></head>
<body>
<h1>Document Ingest API</h1>
<p class="tagline">Turn PDFs, contracts, scans into clean Markdown via one API call.</p>

<h2>Quickstart</h2>
<pre><code>curl -X POST http://localhost:8000/v1/parse \\
  -H "X-API-Key: dip_..." \\
  -F "file=@report.pdf"</code></pre>

<h2>Pricing <span class="badge">pricing preview</span></h2>
<table>
<tr><th>Plan</th><th>Quota</th><th>Price</th></tr>
{pricing}
</table>
<p class="note">Prices are a preview; real billing integration is planned.</p>

<h2>Links</h2>
<ul>
<li><a href="/docs">API Reference (Swagger)</a></li>
<li><a href="https://github.com/user/doc-ingest-api">GitHub</a></li>
<li><a href="/openapi.json">OpenAPI Spec</a></li>
</ul>

<h2>Try it free</h2>
<pre><code>curl -X POST http://localhost:8000/v1/demo-keys</code></pre>
<p class="note">No signup required. Demo keys get 20 pages/month.</p>
</body></html>""")

    # --- Demo sandbox ---

    @router.post("/v1/demo-keys", tags=["demo"])
    def create_demo_key(
        request: Request,
        session: Annotated[Session, Depends(_get_db)],
    ) -> dict[str, Any]:
        """Create a no-signup demo API key (20 pages, 24-hour expiry)."""
        state = _app_state(request)
        raw_key = generate_api_key()
        now = datetime.now(UTC)
        expires = now + timedelta(hours=24)
        with state.session_factory.begin() as db:
            db.add(
                ApiKey(
                    name="demo",
                    key_hash=hash_api_key(raw_key),
                    monthly_page_quota=20,
                    used_pages=0,
                    active=True,
                    plan="demo",
                    expires_at=expires,
                )
            )
        return {
            "api_key": raw_key,
            "plan": "demo",
            "monthly_page_quota": 20,
            "expires_at": expires.isoformat(),
            "upgrade_url": "/#pricing",
            "sample_file_url": "/samples/demo.pdf",
        }

    @router.get("/samples/demo.pdf", tags=["demo"], include_in_schema=False)
    def serve_demo_sample() -> FileResponse:
        """Serve the built-in demo sample PDF."""
        sample_path = Path("assets/samples/demo.pdf")
        return FileResponse(
            path=sample_path,
            media_type="application/pdf",
            filename="demo.pdf",
        )

    return app

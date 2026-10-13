"""FastAPI gateway for document parsing."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
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
from .exceptions import (
    OcrNotConfiguredError,
    ParserError,
    ParseResult,
    ParserTimeoutError,
)
from .models import ApiKey, ParseJob
from .parsers import (
    ParserRouter,
    create_parser_router,
)
from .quotas import ensure_monthly_period, record_page_usage
from .rate_limit import InMemoryRateLimiter, RateLimiter
from .security import find_api_key, generate_api_key, hash_api_key
from .tasks import build_task_queue

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


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
        paddle_env=settings.paddle_env,
        paddle_api_key=settings.paddle_api_key,
        paddle_webhook_secret=settings.paddle_webhook_secret,
        paddle_client_token=settings.paddle_client_token,
    )
    if rate_limiter is None:
        rate_limiter = InMemoryRateLimiter()

    app = FastAPI(
    title="Document Ingest API",
    version="0.2.0",
    docs_url="/docs/swagger",
    redoc_url=None,
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
        # Public informational endpoints are exempt: they must never block
        # first-time visitors, uptime probes, or the docs site.
        if (
            x_api_key is None
            and request.method == "GET"
            and request.url.path
            in {
                "/",
                "/healthz",
                "/openapi.json",
                "/docs",
                "/redoc",
                "/samples/demo.pdf",
                "/billing/success",
                "/docs/swagger",
            }
        ):
            return await call_next(request)
        # Webhook receiver is authenticated by HMAC signature verification, so
        # it must never be blocked by IP rate limiting (Paddle retries depend
        # on fast 200 responses and fires many events per checkout).
        if request.method == "POST" and request.url.path == "/v1/billing/webhook":
            return await call_next(request)
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
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a checkout session for the configured billing provider."""
        state = _app_state(request)
        plan = (body or {}).get("plan", "starter")
        if plan not in {"starter", "pro"}:
            raise _api_error(422, "invalid_plan", "plan must be starter or pro")
        if state.billing_provider.provider_name == "paddle":
            price_id = (
                state.settings.paddle_price_starter
                if plan == "starter"
                else state.settings.paddle_price_pro
            )
        else:
            price_id = f"price_test_{plan}"
        session_result = state.billing_provider.create_checkout_session(
            customer_id=f"cus_test_{api_key.id:08d}",
            price_id=price_id,
            api_key_id=api_key.id,
            success_url="https://tomdocs.com/billing/success",
            cancel_url="https://tomdocs.com/",
        )
        return {
            "billing_provider": session_result.provider,
            "checkout_session_id": session_result.id,
            "checkout_url": session_result.url,
            "customer_id": session_result.customer_id,
            "price_id": session_result.price_id,
        }

    @router.post("/v1/billing/webhook", tags=["billing"])
    async def billing_webhook(
        request: Request,
        session: Annotated[Session, Depends(_get_db)],
    ) -> dict[str, Any]:
        """Verify webhook signature and update subscription state.

        Paddle mode: verifies HMAC-SHA256 signature, processes subscription
        events, and syncs plan/quota to the local API key. Mock mode keeps
        the existing permissive behaviour.
        """
        state = _app_state(request)
        raw_body = await request.body()

        if state.settings.paddle_env in ("sandbox", "live"):
            signature = request.headers.get("paddle-signature", "")
            from .paddle_billing import verify_paddle_webhook_signature

            if not verify_paddle_webhook_signature(
                raw_body=raw_body,
                signature_header=signature,
                secret=state.settings.paddle_webhook_secret,
            ):
                raise _api_error(401, "invalid_signature", "Webhook signature verification failed")

            try:
                event = json.loads(raw_body)
            except json.JSONDecodeError:
                raise _api_error(400, "invalid_payload", "Webhook body is not valid JSON") from None

            event_id = event.get("event_id", "")
            event_type = event.get("event_type", "")
            event_data = event.get("data", {})
            custom = event_data.get("custom_data") or {}
            api_key_id = custom.get("api_key_id")

            plan_quotas = {"free": 5, "starter": 500, "pro": 5000}
            paddle_customer_id = event_data.get("customer_id", "")
            if event_type in ("subscription.created", "subscription.updated", "subscription.canceled") and api_key_id:
                if event_type == "subscription.canceled":
                    plan = "free"
                else:
                    items = event_data.get("items", [])
                    plan = "free"
                    if items:
                        price_custom = items[0].get("price", {}).get("custom_data") or {}
                        plan = str(price_custom.get("plan", "free"))
                quota = plan_quotas.get(plan, 5)
                with state.session_factory.begin() as db:
                    key = db.get(ApiKey, int(api_key_id))
                    if key is not None:
                        key.plan = plan
                        key.monthly_page_quota = quota
                        if paddle_customer_id:
                            key.paddle_customer_id = str(paddle_customer_id)

            return {
                "billing_provider": "paddle",
                "verified": True,
                "event_type": event_type,
                "event_id": event_id,
            }

        # Mock mode (existing behaviour)
        signature = request.headers.get("stripe-signature", "")
        if not signature:
            raise _api_error(400, "missing_signature", "Stripe-Signature header is required")
        verification = state.billing_provider.verify_webhook_signature(
            payload=raw_body,
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
        """Return subscription status and plan details."""
        state = _app_state(request)
        if state.billing_provider.provider_name == "paddle":
            # Webhooks already sync plan/quota onto the local key; querying
            # Paddle by a mock-style id would 404 and surface as a 500.
            return {
                "billing_provider": "paddle",
                "subscription_id": None,
                "status": "active" if api_key.active else "canceled",
                "plan_name": api_key.plan,
                "monthly_page_quota": api_key.monthly_page_quota,
                "used_pages": api_key.used_pages,
            }
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

    @router.post("/v1/keys/recover", tags=["demo"])
    async def recover_keys(
        request: Request,
        session: Annotated[Session, Depends(_get_db)],
    ) -> dict[str, Any]:
        """Recover lost API keys by email: issue new keys, deactivate old ones.

        Security tradeoff: returns 404 uniformly for unknown emails. An
        attacker can only generate new sessions, never recover old keys.
        """
        state = _app_state(request)
        try:
            body = await request.json()
        except Exception:
            raise _api_error(422, "invalid_email", "Provide a valid email address") from None
        email = body.get("email") if isinstance(body, dict) else None
        if not email or not isinstance(email, str) or not _EMAIL_RE.match(email):
            raise _api_error(422, "invalid_email", "Provide a valid email address")

        from sqlalchemy import select

        with state.session_factory.begin() as db:
            rows = db.execute(
                select(ApiKey).where(
                    ApiKey.owner_email == email,
                    ApiKey.active == True,  # noqa: E712
                )
            ).scalars().all()
            if not rows:
                raise _api_error(404, "email_not_found", "No active keys found for this email")

            recovered: list[dict[str, Any]] = []
            for old_key in rows:
                raw_key = generate_api_key()
                new_key = ApiKey(
                    name=old_key.name,
                    key_hash=hash_api_key(raw_key),
                    monthly_page_quota=old_key.monthly_page_quota,
                    used_pages=0,
                    active=True,
                    plan=old_key.plan,
                    expires_at=old_key.expires_at,
                    paddle_customer_id=old_key.paddle_customer_id,
                    owner_email=email,
                )
                db.add(new_key)
                old_key.active = False
                db.flush()
                recovered.append(
                    {
                        "key": raw_key,
                        "plan": new_key.plan,
                        "monthly_page_quota": new_key.monthly_page_quota,
                    }
                )
        return {"keys": recovered}

    @router.post("/v1/billing/portal", tags=["billing"])
    def create_portal(
        request: Request,
        api_key: Annotated[ApiKey, Depends(_get_api_key)],
    ) -> dict[str, Any]:
        """Create a customer portal session for subscription management."""
        state = _app_state(request)
        if state.settings.paddle_env in ("sandbox", "live"):
            customer_id = api_key.paddle_customer_id
            if not customer_id:
                raise _api_error(404, "no_subscription", "No active subscription found; subscribe first")
            portal_url = state.billing_provider.create_portal_session(customer_id)
            return {
                "billing_provider": "paddle",
                "portal_url": portal_url,
            }
        portal_url = state.billing_provider.create_portal_session(f"cus_test_{api_key.id:08d}")
        return {
            "billing_provider": state.billing_provider.provider_name,
            "portal_url": portal_url,
        }

    @router.get("/v1/billing/config", tags=["billing"])
    def billing_config(
        request: Request,
    ) -> dict[str, Any]:
        """Return public billing configuration for Paddle.js checkout."""
        state = _app_state(request)
        result: dict[str, Any] = {}
        if hasattr(state.billing_provider, "get_config"):
            result.update(dict(state.billing_provider.get_config()))
        else:
            result["client_token"] = ""
            result["environment"] = "mock"
        result["price_ids"] = {
            "starter": state.settings.paddle_price_starter,
            "pro": state.settings.paddle_price_pro,
                }
        return result
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
            plan_key = name.lower().replace(" ", "-")
            rows.append(
                f'<tr><td>{name}</td><td>{pages} pages/mo</td>'
                f'<td id="price-{plan_key}">${price}/mo</td></tr>'
            )
        return "\n".join(rows)

    @router.get("/", tags=["health"], response_class=HTMLResponse, include_in_schema=False)
    def landing() -> HTMLResponse:
        """Serve a self-contained landing page with pricing."""
        return HTMLResponse(content="""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DoIngest — Turn Documents into Clean Markdown via One API Call</title>
<meta name="description" content="Multi-engine document parsing API with confidence-based routing. PDF, DOCX, XLSX, PPTX to Markdown.">
<style>
:root{--primary:#457b9d;--primary-dark:#1d3557;--accent:#e63946;--bg:#f8f9fa;--bg-alt:#e9ecef;--text:#1a1a2e;--text-light:#555;--border:#dee2e6;--radius:8px;--shadow:0 2px 8px rgba(0,0,0,.08)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;color:var(--text);background:var(--bg);line-height:1.6}
a{color:var(--primary);text-decoration:none} a:hover{text-decoration:underline}
.container{max-width:1080px;margin:0 auto;padding:0 1.5rem}
/* Nav */
nav{background:var(--primary-dark);padding:.8rem 0}
nav .container{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem}
nav .brand{color:#fff;font-weight:700;font-size:1.1rem}
nav .links{display:flex;gap:1.2rem;flex-wrap:wrap}
nav .links a{color:#e9ecef;font-size:.9rem}
/* Hero */
.hero{background:linear-gradient(135deg,var(--primary-dark) 0%,var(--primary) 100%);color:#fff;padding:4rem 0 3rem;text-align:center}
.hero h1{font-size:2.5rem;margin-bottom:.8rem;line-height:1.2}
.hero p{font-size:1.15rem;color:#cbd5e1;margin-bottom:1.5rem;max-width:640px;margin-inline:auto}
.hero .ctas{display:flex;gap:1rem;justify-content:center;flex-wrap:wrap}
.btn{display:inline-block;padding:.7rem 1.8rem;border-radius:var(--radius);font-size:.95rem;font-weight:600;cursor:pointer;border:none;text-decoration:none!important}
.btn-primary{background:#fff;color:var(--primary-dark)}
.btn-outline{background:transparent;color:#fff;border:2px solid #fff}
.btn:hover{opacity:.9}
/* Terminal */
.terminal{background:#1a1a2e;color:#a8dadc;border-radius:var(--radius);padding:1.2rem;margin:2rem auto 0;max-width:680px;text-align:left;box-shadow:var(--shadow);overflow-x:auto}
.terminal .dots{display:flex;gap:6px;margin-bottom:.8rem}
.terminal .dots span{width:10px;height:10px;border-radius:50%;display:inline-block}
.terminal .dots .r{background:#ff5f56}.terminal .dots .y{background:#ffbd2e}.terminal .dots .g{background:#27c93f}
.terminal pre{font-family:monospace;font-size:.85rem;white-space:pre}
.terminal .c-key{color:#e63946}.terminal .c-str{color:#f1fa8c}.terminal .c-flag{color:#7c7cf8}
/* Sections */
section{padding:3.5rem 0}
section h2{font-size:1.8rem;margin-bottom:2rem;text-align:center}
/* Features */
.features-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1.5rem}
.feature-card{background:#fff;border-radius:var(--radius);padding:1.5rem;box-shadow:var(--shadow)}
.feature-card .icon{width:40px;height:40px;margin-bottom:.8rem}
.feature-card h3{font-size:1rem;margin-bottom:.4rem}
.feature-card p{font-size:.88rem;color:var(--text-light)}
/* Steps */
.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem;text-align:center}
.step{background:#fff;border-radius:var(--radius);padding:1.5rem;box-shadow:var(--shadow)}
.step .num{display:inline-block;width:32px;height:32px;border-radius:50%;background:var(--primary);color:#fff;line-height:32px;font-weight:700;margin-bottom:.6rem}
.step h3{font-size:.95rem}.step p{font-size:.85rem;color:var(--text-light)}
/* Pricing */
.pricing-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem;align-items:stretch}
.plan-card{background:#fff;border-radius:var(--radius);padding:1.8rem;box-shadow:var(--shadow);display:flex;flex-direction:column;position:relative}
.plan-card.featured{border:2px solid var(--primary);transform:scale(1.03)}
.plan-card .badge{position:absolute;top:-12px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;padding:.2rem .8rem;border-radius:12px;font-size:.75rem;font-weight:700}
.plan-card h3{font-size:1.1rem;margin-bottom:.5rem}
.plan-card .price{font-size:2rem;font-weight:700;color:var(--primary-dark)}
.plan-card .quota{font-size:.85rem;color:var(--text-light);margin-bottom:1rem}
.plan-card ul{list-style:none;margin-bottom:1.2rem;flex:1}
.plan-card li{font-size:.85rem;padding:.3rem 0;color:var(--text-light)}
.plan-card li::before{content:"\\2713 ";color:var(--primary);font-weight:700}
.plan-card .btn-cta{width:100%;text-align:center;margin-top:auto}
.btn-cta-primary{background:var(--primary);color:#fff}
.btn-cta-secondary{background:var(--bg-alt);color:var(--text)}
.btn-cta-free{background:var(--bg-alt);color:var(--text-light);font-size:.85rem;padding:.5rem 1rem}
/* Key banner & inputs */
.key-banner{display:none;background:#fff3cd;border:1px solid #ffc107;padding:.8rem 1rem;border-radius:var(--radius);margin-bottom:1rem;font-size:.9rem}
.pricing-form{margin-top:1.5rem}
.pricing-form .inputs{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}
.pricing-form label{display:block;font-size:.82rem;font-weight:600;margin-bottom:.2rem}
.pricing-form input{padding:.5rem;border:1px solid var(--border);border-radius:6px;width:220px;font-size:.88rem}
.pricing-form .btn{font-size:.88rem;padding:.6rem 1.4rem}
.note{font-size:.82rem;color:var(--text-light);margin-top:.4rem}
.note a{text-decoration:underline}
/* FAQ */
.faq details{background:#fff;border-radius:var(--radius);margin-bottom:.8rem;box-shadow:var(--shadow)}
.faq summary{padding:1rem 1.2rem;font-weight:600;font-size:.95rem;cursor:pointer}
.faq summary:hover{color:var(--primary)}
.faq p{padding:0 1.2rem 1rem;font-size:.88rem;color:var(--text-light)}
/* Footer */
footer{background:var(--primary-dark);color:#cbd5e1;padding:2.5rem 0 1.5rem;margin-top:2rem}
footer .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:2rem;margin-bottom:1.5rem}
footer h4{color:#fff;font-size:.9rem;margin-bottom:.6rem}
footer a{color:#cbd5e1;display:block;font-size:.85rem;padding:.15rem 0}
footer .copyright{text-align:center;font-size:.78rem;color:#64748b;border-top:1px solid #2d4a6b;padding-top:1rem;margin-top:1rem}
/* Responsive */
@media(max-width:768px){
  .hero h1{font-size:1.8rem}
  .steps{grid-template-columns:1fr}
  .pricing-grid{grid-template-columns:1fr}
  .plan-card.featured{transform:none}
  footer .grid{grid-template-columns:1fr}
  nav .links{justify-content:center}
}
</style></head>
<body>

<nav>
  <div class="container">
    <span class="brand">DoIngest</span>
    <div class="links">
      <a href="#pricing">Pricing</a>
      <a href="/docs">Docs</a>
      <a href="https://github.com/user/doc-ingest-api">GitHub</a>
      <a href="mailto:support@tomdocs.com">Contact</a>
    </div>
  </div>
</nav>

<div class="hero">
  <div class="container">
    <h1>Turn documents into clean Markdown<br>&mdash; one API call</h1>
    <p>Multi-engine parsing with confidence-based routing. PDF, DOCX, XLSX, PPTX &rarr; structured Markdown.</p>
    <div class="ctas">
      <a href="#pricing" class="btn btn-primary">Get Free API Key</a>
      <a href="/docs" class="btn btn-outline">Read the Docs</a>
    </div>
    <div class="terminal">
      <div class="dots"><span class="r"></span><span class="y"></span><span class="g"></span></div>
      <pre><span class="c-flag">curl</span> -X POST https://tomdocs.com/v1/parse \\
  <span class="c-key">-H</span> <span class="c-str">"X-API-Key: dip_..."</span> \\
  <span class="c-key">-F</span> <span class="c-str">"file=@report.pdf"</span></pre>
    </div>
  </div>
</div>

<section id="features">
  <div class="container">
    <h2>Why DoIngest?</h2>
    <div class="features-grid">
      <div class="feature-card">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="#457b9d" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
        <h3>PDF &amp; Scanned Documents</h3>
        <p>Native PDFs, scanned images, and complex layouts handled by the best-suited engine.</p>
      </div>
      <div class="feature-card">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="#457b9d" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        <h3>Clean Markdown Output</h3>
        <p>Tables, headings, and reading order preserved. Ready for LLM pipelines or CMS ingestion.</p>
      </div>
      <div class="feature-card">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="#457b9d" stroke-width="2"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
        <h3>Simple REST + X-API-Key</h3>
        <p>One header. One POST. No complex OAuth flows or SDK lock-in.</p>
      </div>
      <div class="feature-card">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="#457b9d" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        <h3>MCP Server Integration</h3>
        <p>Works natively with Claude and other AI agents via the Model Context Protocol.</p>
      </div>
      <div class="feature-card">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="#457b9d" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
        <h3>Transparent Per-Page Billing</h3>
        <p>Pay only for pages you parse. Quota tracked in real time, no hidden fees.</p>
      </div>
      <div class="feature-card">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="#457b9d" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        <h3>99.9% Availability Target</h3>
        <p>Hosted on managed cloud infrastructure with automatic scaling and health checks.</p>
      </div>
    </div>
  </div>
</section>

<section id="how-it-works" style="background:#fff">
  <div class="container">
    <h2>How It Works</h2>
    <div class="steps">
      <div class="step"><span class="num">1</span><h3>Get a Key</h3><p>Sign up below with your email. Free tier included.</p></div>
      <div class="step"><span class="num">2</span><h3>Upload a File</h3><p>Send your PDF, DOCX, or image via multipart POST.</p></div>
      <div class="step"><span class="num">3</span><h3>Get Markdown</h3><p>Receive clean, structured Markdown in seconds.</p></div>
    </div>
  </div>
</section>

<section id="pricing">
  <div class="container">
    <h2>Pricing</h2>
    <div class="pricing-grid">
      <div class="plan-card">
        <h3>Free</h3>
        <div class="price">$0</div>
        <div class="quota">5 pages/mo</div>
        <ul>
          <li>Basic parsing</li>
          <li>All file formats</li>
          <li>Community support</li>
        </ul>
        <a href="#try-free" class="btn btn-cta-free btn-cta">Start Free</a>
      </div>
      <div class="plan-card featured">
        <span class="badge">Most Popular</span>
        <h3>Starter</h3>
        <div class="price"><span id="price-starter">$19</span>/mo</div>
        <div class="quota">500 pages/mo</div>
        <ul>
          <li>All parsing features</li>
          <li>Scanned document support</li>
          <li>Email support</li>
        </ul>
        <button id="btn-starter" class="btn btn-cta-primary btn-cta" onclick="openCheckout('starter')">Get Starter &mdash; $19/mo</button>
      </div>
      <div class="plan-card">
        <h3>Pro</h3>
        <div class="price"><span id="price-pro">$99</span>/mo</div>
        <div class="quota">5000 pages/mo</div>
        <ul>
          <li>Everything in Starter</li>
          <li>Priority queue</li>
          <li>Dedicated support</li>
        </ul>
        <button id="btn-pro" class="btn btn-cta-secondary btn-cta" onclick="openCheckout('pro')">Get Pro &mdash; $99/mo</button>
      </div>
    </div>

    <div class="pricing-form" id="try-free">
      <div id="key-banner" class="key-banner"></div>
      <div class="inputs">
        <div>
          <label for="api-key-input">Your API key</label>
          <input id="api-key-input" type="text" placeholder="dip_...">
        </div>
        <div>
          <label for="email-input">Email (for key recovery)</label>
          <input id="email-input" type="email" placeholder="you@example.com">
        </div>
      </div>
      <p class="note">Lost your key? <a href="#" onclick="recoverKeys();return false;">Recover my Key</a> using your email.</p>
      <p class="note">After checkout, your API key plan is upgraded automatically via webhook.</p>
      <p class="note">No signup required. Free keys get 5 pages/month.</p>
    </div>
  </div>
</section>

<section id="faq" style="background:#fff">
  <div class="container">
    <h2>Frequently Asked Questions</h2>
    <div class="faq">
      <details>
        <summary>Is my data safe?</summary>
        <p>Documents are processed in memory and deleted after parsing. API keys are stored as irreversible hashes. We never sell or share your data. See our <a href="/privacy">Privacy Policy</a>.</p>
      </details>
      <details>
        <summary>How are pages counted?</summary>
        <p>Pages are counted from parser metadata (e.g. a 10-page PDF consumes 10 pages of your quota). Monthly quotas reset on the 1st of each UTC month.</p>
      </details>
      <details>
        <summary>Can I cancel my subscription?</summary>
        <p>Yes, anytime via the <a href="#" onclick="openPortal();return false;">customer portal</a> or by emailing support. Cancellation takes effect at the end of the current billing period.</p>
      </details>
      <details>
        <summary>What file formats are supported?</summary>
        <p>PDF, DOCX, XLSX, PPTX, and scanned images. The Service automatically routes to the best parsing engine for each format.</p>
      </details>
      <details>
        <summary>How is this different from open-source parsers?</summary>
        <p>DoIngest runs multiple engines with confidence-based routing, handles cloud OCR for scans, and provides managed infrastructure with SLA. No server maintenance or dependency management.</p>
      </details>
      <details>
        <summary>How do I get support?</summary>
        <p>Email <a href="mailto:support@tomdocs.com">support@tomdocs.com</a>. Starter and Pro users receive priority responses.</p>
      </details>
    </div>
  </div>
</section>

<footer>
  <div class="container">
    <div class="grid">
      <div>
        <h4>Product</h4>
        <a href="#pricing">Pricing</a>
        <a href="/docs">Documentation</a>
      </div>
      <div>
        <h4>Legal</h4>
        <a href="/terms">Terms &amp; Conditions</a>
        <a href="/refund">Refund Policy</a>
        <a href="/privacy">Privacy Policy</a>
      </div>
      <div>
        <h4>Contact</h4>
        <a href="mailto:support@tomdocs.com">support@tomdocs.com</a>
        <a href="https://github.com/user/doc-ingest-api">GitHub</a>
      </div>
    </div>
    <div class="copyright">&copy; 2026 Shandong Tengyu Electronic Technology Co., Ltd. All rights reserved.</div>
  </div>
</footer>

<script src="https://cdn.paddle.com/paddle/v2/paddle.js"></script>
<script>
async function loadBillingConfig() {
  try {
    const res = await fetch("/v1/billing/config");
    const cfg = await res.json();
    if (cfg.client_token && cfg.environment !== "mock") {
      Paddle.Environment.set(cfg.environment);
      Paddle.Initialize({ token: cfg.client_token });
      window._paddleCfg = cfg;
      try {
        Paddle.PricePreview({
          items: [
            { priceId: cfg.price_ids.starter, quantity: 1 },
            { priceId: cfg.price_ids.pro, quantity: 1 },
          ],
        }).then(function(result) {
          if (result && result.lineItems) {
            result.lineItems.forEach(function(item, i) {
              var planKey = i === 0 ? "starter" : "pro";
              var total = item.formattedTotals && item.formattedTotals.total;
              if (!total) return;
              var priceCell = document.getElementById("price-" + planKey);
              if (priceCell) priceCell.textContent = total;
              var btn = document.getElementById("btn-" + planKey);
              if (btn) {
                var planName = planKey.charAt(0).toUpperCase() + planKey.slice(1);
                btn.innerHTML = "Get " + planName + " &mdash; " + total + "/mo";
              }
            });
          }
        }).catch(function() { /* keep fallback */ });
      } catch(pe) { /* keep fallback */ }
    }
  } catch(e) { console.warn("billing config unavailable"); }
}
function showKeyBanner(key, plan) {
  const banner = document.getElementById("key-banner");
  if (banner) {
    banner.innerHTML = "<strong>\\u26a0 Your API Key (copy it now, shown only once):</strong><br><code style=\\"font-size:1rem;color:#e63946\\">" + key + "</code><br><em>Plan: " + plan + "</em>";
    banner.style.display = "block";
  }
}
async function openCheckout(plan) {
  const apiKeyInput = document.getElementById("api-key-input");
  const emailInput = document.getElementById("email-input");
  let apiKey = apiKeyInput ? apiKeyInput.value.trim() : "";
  const email = emailInput ? emailInput.value.trim() : "";
  if (!apiKey) {
    if (!email) { alert("Enter your API key or email address first"); return; }
    try {
      const res = await fetch("/v1/demo-keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email }),
      });
      if (!res.ok) { alert("Failed to create key"); return; }
      const data = await res.json();
      apiKey = data.api_key;
      if (apiKeyInput) apiKeyInput.value = apiKey;
      showKeyBanner(apiKey, data.plan);
    } catch (e) { alert("Failed to create key: network error"); return; }
  }
  if (!window._paddleCfg) { alert("Billing not configured"); return; }
  try {
    const res = await fetch("/v1/billing/checkout", {
      method: "POST",
      headers: { "X-API-Key": apiKey, "Content-Type": "application/json" },
      body: JSON.stringify({ plan: plan }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert("Checkout failed: " + ((err.detail && err.detail.message) || res.status));
      return;
    }
    const data = await res.json();
    const checkoutOpts = { transactionId: data.checkout_session_id };
    if (email) { checkoutOpts.customer = { email: email }; }
    Paddle.Checkout.open(checkoutOpts);
  } catch (e) { alert("Checkout failed: network error"); }
}
async function recoverKeys() {
  const emailInput = document.getElementById("email-input");
  const email = emailInput ? emailInput.value.trim() : "";
  if (!email) { alert("Enter your email address first"); return; }
  try {
    const res = await fetch("/v1/keys/recover", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email }),
    });
    if (res.status === 404) { alert("No keys found for this email address"); return; }
    if (!res.ok) { alert("Recovery failed: " + res.status); return; }
    const data = await res.json();
    if (data.keys && data.keys.length > 0) {
      const k = data.keys[0];
      const apiKeyInput = document.getElementById("api-key-input");
      if (apiKeyInput) apiKeyInput.value = k.key;
      showKeyBanner(k.key, k.plan + " (" + k.monthly_page_quota + " pages/mo)");
    }
  } catch (e) { alert("Recovery failed: network error"); }
}
async function openPortal() {
  const apiKeyInput = document.getElementById("api-key-input");
  const apiKey = apiKeyInput ? apiKeyInput.value.trim() : "";
  if (!apiKey) { alert("Enter your API key first"); return; }
  try {
    const res = await fetch("/v1/billing/portal", {
      method: "POST",
      headers: { "X-API-Key": apiKey },
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert("Portal error: " + ((err.detail && err.detail.message) || res.status));
      return;
    }
    const data = await res.json();
    window.open(data.portal_url, "_blank");
  } catch (e) { alert("Portal error: network error"); }
}
loadBillingConfig();
</script>
</body></html>""")


    @router.get("/docs", tags=["health"], response_class=HTMLResponse, include_in_schema=False)
    def api_docs() -> HTMLResponse:
        """Serve branded API documentation page."""
        return HTMLResponse(content=r'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DoIngest API Documentation</title>
<meta name="description" content="Document Ingest API reference — authentication, endpoints, error codes, rate limits, SDK, and MCP integration.">
<style>
:root{--primary:#457b9d;--primary-dark:#1d3557;--accent:#e63946;--bg:#f8f9fa;--bg-alt:#e9ecef;--text:#1a1a2e;--text-light:#555;--border:#dee2e6;--radius:8px;--shadow:0 2px 8px rgba(0,0,0,.08)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',system-ui,-apple-system,sans-serif;color:var(--text);background:var(--bg);line-height:1.65}
a{color:var(--primary);text-decoration:none}a:hover{text-decoration:underline}
.container{max-width:1080px;margin:0 auto;padding:0 1.5rem}
nav{background:var(--primary-dark);padding:.8rem 0}
nav .container{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem}
nav .brand{color:#fff;font-weight:700;font-size:1.1rem}
nav .links{display:flex;gap:1.2rem;flex-wrap:wrap}
nav .links a{color:#e9ecef;font-size:.9rem}
.doc-hero{background:var(--bg-alt);padding:3rem 0 2rem;text-align:center}
.doc-hero h1{font-size:2rem;color:var(--primary-dark);margin-bottom:.5rem}
.doc-hero p{color:var(--text-light);max-width:560px;margin:0 auto 1.2rem}
.btn{display:inline-block;padding:.6rem 1.5rem;border-radius:var(--radius);font-size:.9rem;font-weight:600;cursor:pointer;border:none}
.btn-primary{background:var(--primary);color:#fff}
.btn-outline{background:transparent;color:var(--primary);border:2px solid var(--primary)}
.btn:hover{opacity:.9}
.layout{display:grid;grid-template-columns:220px 1fr;gap:2.5rem;padding:2.5rem 0}
.toc{position:sticky;top:1rem;align-self:start;max-height:calc(100vh - 2rem);overflow-y:auto}
.toc h3{font-size:.8rem;text-transform:uppercase;letter-spacing:.5px;color:var(--text-light);margin-bottom:.6rem}
.toc a{display:block;padding:.25rem 0;font-size:.85rem;color:var(--text-light);border-left:2px solid transparent;padding-left:.6rem}
.toc a:hover{color:var(--primary);border-left-color:var(--primary);text-decoration:none}
.doc-content{min-width:0}
.doc-content section{margin-bottom:2.5rem;padding:0}
.doc-content h2{font-size:1.4rem;color:var(--primary-dark);border-bottom:2px solid var(--bg-alt);padding-bottom:.4rem;margin-bottom:1rem}
.doc-content h3{font-size:1rem;margin:1.2rem 0 .4rem}
.doc-content p{font-size:.9rem;margin:.5rem 0}
.doc-content code{background:var(--bg-alt);padding:.15rem .4rem;border-radius:4px;font-size:.85rem;font-family:monospace}
.doc-content pre{background:#1a1a2e;color:#a8dadc;padding:1rem;border-radius:var(--radius);overflow-x:auto;font-size:.83rem;margin:.8rem 0}
.doc-content pre code{background:transparent;padding:0;font-size:.83rem}
.doc-content table{width:100%;border-collapse:collapse;margin:.8rem 0;font-size:.85rem}
.doc-content th,.doc-content td{border:1px solid var(--border);padding:.5rem .7rem;text-align:left}
.doc-content th{background:var(--bg-alt);font-weight:600}
.doc-content .method{display:inline-block;padding:.1rem .5rem;border-radius:4px;font-size:.75rem;font-weight:700;font-family:monospace}
.m-get{background:#d4edda;color:#155724}
.m-post{background:#d1ecf1;color:#0c5460}
.tag{display:inline-block;background:var(--bg-alt);color:var(--text-light);padding:.05rem .4rem;border-radius:3px;font-size:.75rem}
.cta-box{background:var(--primary-dark);color:#fff;border-radius:var(--radius);padding:2rem;text-align:center;margin-top:2.5rem}
.cta-box p{font-size:1.05rem;margin-bottom:1rem;color:#cbd5e1}
.cta-box .btn{background:#fff;color:var(--primary-dark)}
footer{background:var(--primary-dark);color:#cbd5e1;padding:2rem 0 1rem;margin-top:2rem}
footer .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.5rem;margin-bottom:1rem}
footer h4{color:#fff;font-size:.85rem;margin-bottom:.5rem}
footer a{color:#cbd5e1;display:block;font-size:.82rem;padding:.1rem 0}
footer .copyright{text-align:center;font-size:.75rem;color:#64748b;border-top:1px solid #2d4a6b;padding-top:.8rem;margin-top:.8rem}
@media(max-width:768px){.layout{grid-template-columns:1fr}.toc{position:static;max-height:none;display:none}footer .grid{grid-template-columns:1fr}.doc-hero h1{font-size:1.5rem}}
</style></head>
<body>
<nav><div class="container"><span class="brand">DoIngest</span><div class="links"><a href="/#pricing">Pricing</a><a href="/docs">Docs</a><a href="https://github.com/user/doc-ingest-api">GitHub</a><a href="mailto:support@tomdocs.com">Contact</a></div></div></nav>
<div class="doc-hero"><div class="container">
  <h1>DoIngest API Documentation</h1>
  <p>Multi-engine document parsing with confidence-based routing. Convert PDF, DOCX, XLSX, and PPTX into clean Markdown.</p>
  <a href="/docs/swagger" class="btn btn-primary" style="margin-right:.5rem">Open Interactive Docs</a>
  <a href="/#try-free" class="btn btn-outline">Get Free API Key</a>
</div></div>

<div class="container"><div class="layout">
<aside class="toc">
  <h3>On this page</h3>
  <a href="#quickstart">Quickstart</a>
  <a href="#authentication">Authentication</a>
  <a href="#endpoints">Endpoints</a>
  <a href="#parse-detail">POST /v1/parse</a>
  <a href="#errors">Error Codes</a>
  <a href="#rate-limits">Rate Limits</a>
  <a href="#sdk-mcp">SDK &amp; MCP</a>
</aside>

<main class="doc-content">

<section id="quickstart">
  <h2>Quickstart</h2>
  <p>Parse a document in one API call:</p>
  <pre><code>curl -X POST https://tomdocs.com/v1/parse \
  -H "X-API-Key: dip_your_key_here" \
  -F "file=@report.pdf"</code></pre>
  <p>Response:</p>
  <pre><code>{
  "markdown": "# Chapter 1\n\nParsed content...",
  "metadata": {
    "engine_used": "markitdown",
    "confidence": 0.95,
    "page_count": 3,
    "duration_ms": 1420.5
  }
}</code></pre>
</section>

<section id="authentication">
  <h2>Authentication</h2>
  <p>All API endpoints require an <code>X-API-Key</code> header. Keys are prefixed with <code>dip_</code> and look like <code>dip_AbC123XyZ...</code>.</p>
  <table>
    <tr><th>Key type</th><th>Pages/mo</th><th>Validity</th></tr>
    <tr><td>Free</td><td>5</td><td>Until cancelled</td></tr>
    <tr><td>Demo</td><td>20</td><td>24 hours</td></tr>
    <tr><td>Starter</td><td>500</td><td>Active subscription</td></tr>
    <tr><td>Pro</td><td>5000</td><td>Active subscription</td></tr>
  </table>
</section>

<section id="endpoints">
  <h2>Endpoints</h2>
  <table>
    <tr><th>Method</th><th>Path</th><th>Description</th><th>Auth</th></tr>
    <tr><td><span class="method m-get">GET</span></td><td><code>/healthz</code></td><td>Liveness probe</td><td>No</td></tr>
    <tr><td><span class="method m-post">POST</span></td><td><code>/v1/demo-keys</code></td><td>Create demo key (20 pages/24h)</td><td>No</td></tr>
    <tr><td><span class="method m-post">POST</span></td><td><code>/v1/parse</code></td><td>Parse a document synchronously</td><td><span class="tag">API Key</span></td></tr>
    <tr><td><span class="method m-post">POST</span></td><td><code>/v1/parse/async</code></td><td>Queue a document for parsing</td><td><span class="tag">API Key</span></td></tr>
    <tr><td><span class="method m-get">GET</span></td><td><code>/v1/jobs/{job_id}</code></td><td>Check async job status</td><td><span class="tag">API Key</span></td></tr>
    <tr><td><span class="method m-get">GET</span></td><td><code>/v1/billing/subscription</code></td><td>Get subscription status</td><td><span class="tag">API Key</span></td></tr>
    <tr><td><span class="method m-post">POST</span></td><td><code>/v1/billing/checkout</code></td><td>Create checkout session</td><td><span class="tag">API Key</span></td></tr>
    <tr><td><span class="method m-post">POST</span></td><td><code>/v1/billing/portal</code></td><td>Open customer portal</td><td><span class="tag">API Key</span></td></tr>
    <tr><td><span class="method m-get">GET</span></td><td><code>/v1/billing/config</code></td><td>Public billing config (Paddle.js)</td><td>No</td></tr>
    <tr><td><span class="method m-post">POST</span></td><td><code>/v1/keys/recover</code></td><td>Recover lost keys by email</td><td>No</td></tr>
  </table>
</section>

<section id="parse-detail">
  <h2>POST /v1/parse</h2>
  <h3>Request (multipart/form-data)</h3>
  <table>
    <tr><th>Field</th><th>Type</th><th>Required</th><th>Description</th></tr>
    <tr><td><code>file</code></td><td>binary</td><td>Yes</td><td>Document file (PDF, DOCX, XLSX, PPTX)</td></tr>
    <tr><td><code>category</code></td><td>string</td><td>No</td><td>Hint for parser routing</td></tr>
  </table>
  <h3>Response</h3>
  <pre><code>{
  "markdown": "...",
  "metadata": {
    "engine_used": "anydoc",
    "confidence": 0.95,
    "page_count": 3,
    "duration_ms": 1420.5
  }
}</code></pre>
  <p>Demo keys also receive <code>"upgrade_url": "/#pricing"</code>.</p>
</section>

<section id="errors">
  <h2>Error Codes</h2>
  <table>
    <tr><th>HTTP</th><th>Code</th><th>Description</th></tr>
    <tr><td>400</td><td><code>missing_signature</code></td><td>Webhook signature header absent (mock mode)</td></tr>
    <tr><td>400</td><td><code>invalid_payload</code></td><td>Webhook body is not valid JSON</td></tr>
    <tr><td>401</td><td><code>missing_api_key</code></td><td>No <code>X-API-Key</code> header</td></tr>
    <tr><td>401</td><td><code>invalid_api_key</code></td><td>Key not found, inactive, or expired</td></tr>
    <tr><td>401</td><td><code>invalid_signature</code></td><td>Webhook HMAC verification failed</td></tr>
    <tr><td>404</td><td><code>job_not_found</code></td><td>Parse job does not exist</td></tr>
    <tr><td>404</td><td><code>no_subscription</code></td><td>No active subscription for portal</td></tr>
    <tr><td>404</td><td><code>email_not_found</code></td><td>No active keys for this email</td></tr>
    <tr><td>422</td><td><code>invalid_email</code></td><td>Email format validation failed</td></tr>
    <tr><td>422</td><td><code>invalid_plan</code></td><td>Checkout plan must be starter or pro</td></tr>
    <tr><td>429</td><td><code>rate_limited</code></td><td>IP or key rate limit exceeded</td></tr>
    <tr><td>429</td><td><code>quota_exceeded</code></td><td>Monthly page quota exhausted</td></tr>
    <tr><td>502</td><td><code>parser_failed</code></td><td>All parsers failed</td></tr>
    <tr><td>503</td><td><code>ocr_not_configured</code></td><td>Scanned document but no OCR key</td></tr>
    <tr><td>504</td><td><code>parser_timeout</code></td><td>Parser exceeded timeout</td></tr>
  </table>
</section>

<section id="rate-limits">
  <h2>Rate Limits</h2>
  <table>
    <tr><th>Scope</th><th>Limit</th><th>Window</th></tr>
    <tr><td>Per IP (unauthenticated)</td><td>50 requests</td><td>1 hour</td></tr>
    <tr><td>Per API key</td><td>60 requests</td><td>1 minute</td></tr>
  </table>
  <p>Exceeded limits return <code>429</code> with a <code>Retry-After</code> header. Successful responses include <code>X-RateLimit-Remaining</code>.</p>
</section>

<section id="sdk-mcp">
  <h2>SDK &amp; MCP</h2>
  <h3>Python SDK</h3>
  <pre><code>from doc_ingest_sdk import DocIngestClient

client = DocIngestClient(
    base_url="https://tomdocs.com",
    api_key="dip_...",
)
result = client.parse_document(file_path="report.pdf")
print(result.markdown)</code></pre>
  <h3>MCP Server (AI agents)</h3>
  <pre><code>{
  "mcpServers": {
    "doc-ingest": {
      "command": "uv",
      "args": ["run", "doc-ingest-mcp"],
      "env": {
        "DOC_INGEST_BASE_URL": "https://tomdocs.com",
        "DOC_INGEST_API_KEY": "dip_..."
      }
    }
  }
}</code></pre>
</section>

<div class="cta-box">
  <p>Ready to parse your first document?</p>
  <a href="/#try-free" class="btn">Get Free API Key</a>
</div>

</main>
</div></div>

<footer><div class="container">
  <div class="grid">
    <div><h4>Product</h4><a href="/#pricing">Pricing</a><a href="/docs/swagger">Interactive Docs</a></div>
    <div><h4>Legal</h4><a href="/terms">Terms</a><a href="/refund">Refund Policy</a><a href="/privacy">Privacy Policy</a></div>
    <div><h4>Contact</h4><a href="mailto:support@tomdocs.com">support@tomdocs.com</a><a href="https://github.com/user/doc-ingest-api">GitHub</a></div>
  </div>
  <div class="copyright">&copy; 2026 Shandong Tengyu Electronic Technology Co., Ltd.</div>
</div></footer>
</body></html>''')

    @router.get(
        "/billing/success",
        tags=["health"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def billing_success() -> HTMLResponse:
        """Serve the post-checkout confirmation page."""
        return HTMLResponse(content="""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Payment received — Document Ingest API</title>
<style>body{font-family:system-ui,sans-serif;margin:0;padding:3rem;background:#f8f9fa;color:#1a1a2e}
h1{font-size:1.8rem}.note{color:#555}a{color:#457b9d}</style></head>
<body>
<h1>Payment received</h1>
<p class="note">Your subscription is active. Your API key plan will be upgraded
automatically within a minute.</p>
<p class="note">Verify it any time:</p>
<pre><code>curl https://tomdocs.com/v1/billing/subscription \
  -H "X-API-Key: dip_..."</code></pre>
<p>Lost your API key? <a href="/">Go back to the homepage</a> and use your registered email to recover it.</p>
<p><a href="/">Back to home</a> &middot; <a href="/docs">API reference</a></p>
<div style="margin-top:2rem;padding-top:1rem;border-top:1px solid #ddd;font-size:.85rem;color:#888">
  <a href="/terms">Terms &amp; Conditions</a> &middot;
  <a href="/refund">Refund Policy</a> &middot;
  <a href="/privacy">Privacy Policy</a>
  &middot;
  <a href="mailto:support@tomdocs.com">support@tomdocs.com</a>
</div>
</body></html>""")

    # --- Demo sandbox ---

    @router.post("/v1/demo-keys", tags=["demo"])
    async def create_demo_key(
        request: Request,
        session: Annotated[Session, Depends(_get_db)],
    ) -> dict[str, Any]:
        """Create a no-signup demo API key (20 pages, 24-hour expiry).

        Optional ``email`` in the body binds the key to an owner for recovery.
        """
        state = _app_state(request)
        body: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            body = await request.json()
        email = body.get("email") if isinstance(body, dict) else None
        if email is not None and (not isinstance(email, str) or not _EMAIL_RE.match(email)):
            raise _api_error(422, "invalid_email", "Provide a valid email address")
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
                    owner_email=email,
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


    # --- Legal pages (Paddle domain review requirement) ---

    def _legal_page(title: str, body: str) -> HTMLResponse:
        """Render a self-contained legal page with consistent styling."""
        return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{title} | DoIngest</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;padding:2rem;max-width:800px;margin:auto;background:#f8f9fa;color:#1a1a2e;line-height:1.7}}
h1{{font-size:1.8rem;margin-bottom:.5rem}} h2{{margin-top:1.5rem;font-size:1.2rem}}
p{{margin:.8rem 0}} a{{color:#457b9d}}
footer{{margin-top:2rem;padding-top:1rem;border-top:1px solid #ddd;font-size:.85rem;color:#888}}
</style></head>
<body>
<h1>{title}</h1>
{body}
<footer>
  <a href="/">Back to home</a> &middot;
  <a href="/terms">Terms</a> &middot;
  <a href="/refund">Refund Policy</a> &middot;
  <a href="/privacy">Privacy Policy</a>
  &middot;
  <a href="mailto:support@tomdocs.com">support@tomdocs.com</a>
</footer>
</body></html>""")

    @router.get("/terms", tags=["health"], response_class=HTMLResponse, include_in_schema=False)
    def terms_page() -> HTMLResponse:
        """Serve Terms & Conditions."""
        body = """
<p><strong>Effective date:</strong> October 11, 2026</p>

<p>These Terms &amp; Conditions ("Terms") govern your access to and use of the Document Ingest API service ("Service") provided by DoIngest ("we", "us", "our"), operated by Shandong Tengyu Electronic Technology Co., Ltd. (\u5c71\u4e1c\u6ed5\u5b87\u7535\u5b50\u79d1\u6280\u6709\u9650\u516c\u53f8), registered in Tengzhou, Shandong, China, company number 91370481MAD404MG6Q. By creating an API key, subscribing to a plan, or otherwise using the Service, you agree to be bound by these Terms.</p>

<h2>1. Seller and Payment Processing</h2>
<p>DoIngest is the seller of the Service. Paddle.com acts as the Merchant of Record for all transactions. This means that Paddle.com handles payment processing, invoicing, tax collection, and customer billing on our behalf. Charges on your credit card or bank statement will appear as "Paddle.com". Paddle's own terms of use apply to the payment transaction.</p>

<h2>2. Service Description</h2>
<p>DoIngest provides a web-based API that converts documents (including PDF, DOCX, XLSX, and PPTX files) into clean, structured Markdown text. The Service uses multiple parsing engines with confidence-based routing to deliver the best possible extraction quality.</p>

<h2>3. Subscription and Billing</h2>
<p>The Service is offered on a monthly subscription basis. Plans and pricing are listed on our pricing page. Subscriptions are billed monthly in advance through Paddle checkout. You may cancel your subscription at any time; cancellation takes effect at the end of the current billing period, and you will retain access until then. We do not offer partial-period refunds for unused pages unless required by applicable law or our Refund Policy.</p>

<h2>4. Acceptable Use</h2>
<p>You agree not to:</p>
<p>(a) upload or transmit content that is illegal, harmful, threatening, abusive, defamatory, or otherwise objectionable;<br>
(b) attempt to circumvent usage quotas or rate limits;<br>
(c) publicly share, resell, or sublicense your API keys;<br>
(d) use the Service to build a product that directly competes with DoIngest;<br>
(e) reverse-engineer, decompile, or disassemble any part of the Service.</p>
<p>We reserve the right to suspend or terminate your access if we reasonably believe you have violated these Terms. Where feasible, we will provide notice of the violation and an opportunity to cure.</p>

<h2>5. Intellectual Property</h2>
<p>You retain all rights to the documents you upload and the content you extract. We claim no ownership over your data. The Service software, branding, and documentation are the property of Shandong Tengyu Electronic Technology Co., Ltd.</p>

<h2>6. Disclaimer of Warranties</h2>
<p>THE SERVICE IS PROVIDED "AS IS" WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT. We do not warrant that the Service will be uninterrupted, error-free, or that extracted content will be accurate or complete.</p>

<h2>7. Limitation of Liability</h2>
<p>TO THE MAXIMUM EXTENT PERMITTED BY LAW, SHANDONG TENGYU ELECTRONIC TECHNOLOGY CO., LTD. SHALL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, OR PUNITIVE DAMAGES ARISING FROM YOUR USE OF THE SERVICE. OUR TOTAL AGGREGATE LIABILITY SHALL NOT EXCEED THE AMOUNT YOU PAID US IN THE TWELVE (12) MONTHS PRECEDING THE CLAIM.</p>

<h2>8. Governing Law</h2>
<p>These Terms shall be governed by and construed in accordance with the laws of the People\u2019s Republic of China, without regard to conflict-of-law principles. Any dispute shall be resolved by binding arbitration or in a court of competent jurisdiction in that jurisdiction.</p>

<h2>9. Changes to These Terms</h2>
<p>We may update these Terms from time to time. Material changes will be announced on this page at least seven (7) days before taking effect. Your continued use of the Service after changes take effect constitutes acceptance of the updated Terms.</p>

<h2>10. Contact</h2>
<p>For questions about these Terms, contact us at <a href="mailto:support@tomdocs.com">support@tomdocs.com</a>.</p>
"""
        return _legal_page("Terms &amp; Conditions", body)

    @router.get("/refund", tags=["health"], response_class=HTMLResponse, include_in_schema=False)
    def refund_page() -> HTMLResponse:
        """Serve Refund Policy."""
        body = """
<p><strong>Effective date:</strong> October 11, 2026</p>

<p>This Refund Policy applies to all subscriptions for the DoIngest Document Ingest API Service ("Service"). DoIngest ("we", "us", "our"), operated by Shandong Tengyu Electronic Technology Co., Ltd., is the seller of the Service. Paddle.com acts as the Merchant of Record and handles all payment processing, invoicing, and refunds on our behalf.</p>

<h2>1. Merchant of Record</h2>
<p>Paddle.com processes your payment and is the entity responsible for issuing invoices and processing refunds. When you request a refund, Paddle processes the transaction and the refunded amount will be returned to your original payment method. Charges on your statement will appear as "Paddle.com".</p>

<h2>2. Refund Eligibility</h2>
<p><strong>First-time subscribers:</strong> You may request a full refund within fourteen (14) days of your initial subscription payment, no questions asked.</p>
<p><strong>Subsequent billing cycles:</strong> For months after the first billing period, we generally do not offer refunds for partially used subscription periods. However, if you believe a refund is warranted due to a Service defect, billing error, or exceptional circumstance, contact us and we will review your case in good faith and in accordance with Paddle's refund policy.</p>

<h2>3. How to Request a Refund</h2>
<p>You may request a refund by:</p>
<p>(a) Clicking the refund link in the receipt email sent to you by Paddle after your purchase; or<br>
(b) Emailing us at <a href="mailto:support@tomdocs.com">support@tomdocs.com</a> with your order or subscription reference.</p>
<p>Refunds are typically processed by Paddle within five (5) to ten (10) business days, depending on your payment provider.</p>

<h2>4. Cancellation</h2>
<p>You may cancel your subscription at any time through the customer portal or by emailing us. Cancellation takes effect at the end of the current billing period; you will retain full access until then. There are no cancellation fees.</p>

<h2>5. Chargebacks</h2>
<p>We encourage you to contact us before filing a chargeback with your bank. We will work with you to resolve billing issues promptly. Chargebacks filed without first attempting to contact us may result in account suspension.</p>

<h2>6. Contact</h2>
<p>For questions about refunds, contact us at <a href="mailto:support@tomdocs.com">support@tomdocs.com</a>.</p>
"""
        return _legal_page("Refund Policy", body)

    @router.get("/privacy", tags=["health"], response_class=HTMLResponse, include_in_schema=False)
    def privacy_page() -> HTMLResponse:
        """Serve Privacy Policy."""
        body = """
<p><strong>Effective date:</strong> October 11, 2026</p>

<p>This Privacy Policy describes how DoIngest ("we", "us", "our"), operated by Shandong Tengyu Electronic Technology Co., Ltd., collects, uses, and protects information in connection with the Document Ingest API Service ("Service"). We are committed to protecting your privacy and complying with applicable data protection laws, including the General Data Protection Regulation (GDPR) and the California Consumer Privacy Act (CCPA).</p>

<h2>1. Information We Collect</h2>
<p>We collect the following categories of information:</p>
<p><strong>Account information:</strong> Your email address (collected and managed by Paddle.com during checkout) and a hashed representation of your API key. We do not store your raw API key.</p>
<p><strong>Document content:</strong> When you upload a document for parsing, we temporarily store its contents to perform the conversion. This content is processed solely to deliver the Service result.</p>
<p><strong>Usage metadata:</strong> Page count, processing duration, engine used, timestamps, and error messages. This data is used for billing, abuse detection, and service improvement.</p>
<p><strong>Billing information:</strong> Payment details are handled exclusively by Paddle.com as Merchant of Record. We do not store credit card numbers or bank details.</p>

<h2>2. How We Use Your Information</h2>
<p>We use collected information to:</p>
<p>(a) provide, maintain, and improve the Service;<br>
(b) process payments and prevent billing fraud (via Paddle);<br>
(c) enforce usage quotas and rate limits;<br>
(d) diagnose and fix Service issues;<br>
(e) communicate service updates and security notices.</p>

<h2>3. Third-Party Processors</h2>
<p>We rely on the following third parties to operate the Service:</p>
<p><strong>Paddle.com</strong> acts as Merchant of Record and processes payments, invoices, and customer records. Paddle's own privacy policy governs the payment data they collect. See <a href="https://paddle.com/legal/privacy">https://paddle.com/legal/privacy</a>.</p>
<p><strong>Cloud OCR providers</strong> (such as Alibaba Cloud) may receive scanned document images when the Service routes such documents for OCR. These providers process data solely to return extracted text.</p>
<p><strong>Cloud hosting providers</strong> (Render, Fly.io, or Railway) store application data and process requests. They act as data processors under our instruction.</p>

<h2>4. Data Retention</h2>
<p><strong>Document content:</strong> Uploaded documents and their extracted output are retained only as long as necessary to complete the parsing request. Temporarily stored files are deleted after processing. If a document is queued for asynchronous parsing, it is stored until the job completes and the result is retrieved or expires.</p>
<p><strong>Usage metadata:</strong> Retained for billing accuracy, quota management, and dispute resolution for up to twelve (12) months.</p>
<p><strong>Account records:</strong> Retained for the duration of your subscription and as required by law.</p>

<h2>5. Your Rights</h2>
<p>Depending on your jurisdiction, you may have the right to:</p>
<p>(a) access the personal data we hold about you;<br>
(b) request correction or deletion of your data;<br>
(c) object to or restrict certain processing;<br>
(d) receive a portable copy of your data;<br>
(e) withdraw consent (where processing is based on consent).</p>
<p>To exercise any of these rights, email <a href="mailto:support@tomdocs.com">support@tomdocs.com</a>. We will respond within thirty (30) days.</p>

<h2>6. GDPR</h2>
<p>For users in the European Union and European Economic Area, we act as a data controller for account and usage data, and as a data processor for document content you upload. Our lawful bases for processing include performance of contract (providing the Service), legitimate interests (service improvement and abuse prevention), and legal obligations (billing records).</p>

<h2>7. CCPA</h2>
<p>For California residents, we do not sell or share personal information as defined by the CCPA. You have the right to know what personal information is collected, request deletion, and be free from discrimination for exercising your rights.</p>

<h2>8. Security</h2>
<p>We use industry-standard measures to protect your data, including encrypted connections (HTTPS/TLS), hashed API keys, and access controls. No system is completely secure; we encourage you to use unique API keys and report suspected breaches immediately.</p>

<h2>9. Changes to This Policy</h2>
<p>We may update this Privacy Policy from time to time. Material changes will be announced on this page. Your continued use of the Service after changes take effect constitutes acceptance of the updated policy.</p>

<h2>10. Contact</h2>
<p>For privacy inquiries, contact us at <a href="mailto:support@tomdocs.com">support@tomdocs.com</a>.</p>
"""
        return _legal_page("Privacy Policy", body)

    return app

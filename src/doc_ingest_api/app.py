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
<p class="note">Subscriptions are billed securely by Paddle. Enter your API key below, then pick a plan.</p>

<h2>Links</h2>
<ul>
<li><a href="/docs">API Reference (Swagger)</a></li>
<li><a href="https://github.com/user/doc-ingest-api">GitHub</a></li>
<li><a href="/openapi.json">OpenAPI Spec</a></li>
</ul>

<h2>Try it free</h2>
<pre><code>curl -X POST http://localhost:8000/v1/demo-keys</code></pre>
<p class="note">No signup required. Demo keys get 20 pages/month.</p>

<script src="https://cdn.paddle.com/paddle/v2/paddle.js"></script>
<script>
async function loadBillingConfig() {{
  try {{
    const res = await fetch("/v1/billing/config");
    const cfg = await res.json();
    if (cfg.client_token && cfg.environment !== "mock") {{
      Paddle.Environment.set(cfg.environment);
      Paddle.Initialize({{ token: cfg.client_token }});
      window._paddleCfg = cfg;
      // PricePreview for localized pricing (sandbox/live only)
      try {{
        Paddle.PricePreview({{
          items: [
            {{ priceId: cfg.price_ids.starter, quantity: 1 }},
            {{ priceId: cfg.price_ids.pro, quantity: 1 }},
          ],
        }}).then(function(result) {{
          if (result && result.lineItems) {{
            result.lineItems.forEach(function(item, i) {{
              var planKey = i === 0 ? "starter" : "pro";
              var total = item.formattedTotals && item.formattedTotals.total;
              if (!total) return;
              // Update pricing table price cell
              var priceCell = document.getElementById("price-" + planKey);
              if (priceCell) priceCell.textContent = total + "/mo";
              // Update button text
              var btn = document.getElementById("btn-" + planKey);
              if (btn) {{
                var planName = planKey.charAt(0).toUpperCase() + planKey.slice(1);
                btn.textContent = "Get " + planName + " \u2014 " + total + "/mo";
              }}
            }});
          }}
        }}).catch(function() {{
          // PricePreview failed; keep hardcoded fallback
        }});
      }} catch(pe) {{
        // PricePreview unavailable; keep hardcoded fallback
      }}
    }}
  }} catch(e) {{ console.warn("billing config unavailable"); }}
}}
function showKeyBanner(key, plan) {{
  const banner = document.getElementById("key-banner");
  if (banner) {{
    banner.innerHTML = "<strong>\u26a0 Your API Key (copy it now, shown only once):</strong><br><code style=\"font-size:1rem;color:#e63946\">" + key + "</code><br><em>Plan: " + plan + "</em>";
    banner.style.display = "block";
  }}
}}
async function openCheckout(plan) {{
  const apiKeyInput = document.getElementById("api-key-input");
  const emailInput = document.getElementById("email-input");
  let apiKey = apiKeyInput ? apiKeyInput.value.trim() : "";
  const email = emailInput ? emailInput.value.trim() : "";

  if (!apiKey) {{
    if (!email) {{
      alert("Enter your API key or email address first");
      return;
    }}
    // Create a new demo key with the email
    try {{
      const res = await fetch("/v1/demo-keys", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ email: email }}),
      }});
      if (!res.ok) {{
        alert("Failed to create key");
        return;
      }}
      const data = await res.json();
      apiKey = data.api_key;
      if (apiKeyInput) apiKeyInput.value = apiKey;
      showKeyBanner(apiKey, data.plan);
    }} catch (e) {{
      alert("Failed to create key: network error");
      return;
    }}
  }}

  if (!window._paddleCfg) {{ alert("Billing not configured"); return; }}
  try {{
    const res = await fetch("/v1/billing/checkout", {{
      method: "POST",
      headers: {{ "X-API-Key": apiKey, "Content-Type": "application/json" }},
      body: JSON.stringify({{ plan: plan }}),
    }});
    if (!res.ok) {{
      const err = await res.json().catch(() => ({{}}));
      alert("Checkout failed: " + ((err.detail && err.detail.message) || res.status));
      return;
    }}
    const data = await res.json();
    const checkoutOpts = {{ transactionId: data.checkout_session_id }};
    if (email) {{
      checkoutOpts.customer = {{ email: email }};
    }}
    Paddle.Checkout.open(checkoutOpts);
  }} catch (e) {{
    alert("Checkout failed: network error");
  }}
}}
async function recoverKeys() {{
  const emailInput = document.getElementById("email-input");
  const email = emailInput ? emailInput.value.trim() : "";
  if (!email) {{
    alert("Enter your email address first");
    return;
  }}
  try {{
    const res = await fetch("/v1/keys/recover", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ email: email }}),
    }});
    if (res.status === 404) {{
      alert("No keys found for this email address");
      return;
    }}
    if (!res.ok) {{
      alert("Recovery failed: " + res.status);
      return;
    }}
    const data = await res.json();
    if (data.keys && data.keys.length > 0) {{
      const k = data.keys[0];
      const apiKeyInput = document.getElementById("api-key-input");
      if (apiKeyInput) apiKeyInput.value = k.key;
      showKeyBanner(k.key, k.plan + " (" + k.monthly_page_quota + " pages/mo)");
    }}
  }} catch (e) {{
    alert("Recovery failed: network error");
  }}
}}
loadBillingConfig();
</script>

<div style="margin-top:1rem">
  <div id="key-banner" style="display:none;background:#fff3cd;border:1px solid #ffc107;padding:.8rem;border-radius:6px;margin-bottom:.8rem;font-size:.9rem"></div>
  <div style="display:flex;gap:1rem;flex-wrap:wrap;align-items:flex-end">
    <div>
      <label style="font-size:.85rem">Your API key:</label><br>
      <input id="api-key-input" type="text" placeholder="dip_..."
        style="padding:.4rem;border:1px solid #ccc;border-radius:4px;width:240px">
    </div>
    <div>
      <label style="font-size:.85rem">Email (for key recovery):</label><br>
      <input id="email-input" type="email" placeholder="you@example.com"
        style="padding:.4rem;border:1px solid #ccc;border-radius:4px;width:220px">
    </div>
  </div>
  <div style="margin-top:.8rem">
    <button id="btn-starter" onclick="openCheckout('starter')" style="background:#457b9d;color:#fff;border:none;padding:.6rem 1.5rem;border-radius:6px;cursor:pointer;font-size:.9rem">Get Starter — $19/mo</button>
    <button id="btn-pro" onclick="openCheckout('pro')" style="background:#1d3557;color:#fff;border:none;padding:.6rem 1.5rem;border-radius:6px;cursor:pointer;font-size:.9rem;margin-left:.5rem">Get Pro — $99/mo</button>
  </div>
  <p class="note" style="margin-top:.5rem">
    Lost your key? <a href="#" onclick="recoverKeys();return false;" style="text-decoration:underline">Recover my Key</a> using your email.
  </p>
  <p class="note" style="margin-top:.3rem">After checkout, your API key plan is upgraded automatically via webhook.</p>
</div>
<div style="margin-top:2rem;padding-top:1rem;border-top:1px solid #ddd;font-size:.85rem;color:#888">
  <a href="/terms">Terms &amp; Conditions</a> &middot;
  <a href="/refund">Refund Policy</a> &middot;
  <a href="/privacy">Privacy Policy</a>
  &middot;
  <a href="mailto:support@tomdocs.com">support@tomdocs.com</a>
</div>
</body></html>""")

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

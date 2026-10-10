"""FastAPI gateway for document parsing."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
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
from .security import find_api_key
from .tasks import build_task_queue


@dataclass(frozen=True)
class _AppState:
    """Objects shared with request dependencies."""

    settings: Settings
    session_factory: sessionmaker[Session]
    parser_router: ParserRouter
    billing_provider: Any


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

    app = FastAPI(title="Document Ingest API", version="0.1.0")
    app.state.doc_ingest = _AppState(
        settings=settings,
        session_factory=session_factory,
        parser_router=parser_router,
        billing_provider=billing_provider,
    )
    router = app.router

    @router.get("/healthz")
    def healthz() -> dict[str, str]:
        """Return a lightweight liveness response."""
        return {"status": "ok"}

    @router.post("/v1/parse")
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
        return {"markdown": result.markdown, "metadata": metadata}

    @router.post("/v1/parse/async", status_code=202)
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

    @router.get("/v1/jobs/{job_id}")
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


    @router.post("/v1/billing/checkout")
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

    @router.post("/v1/billing/webhook")
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

    @router.get("/v1/billing/subscription")
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

    return app
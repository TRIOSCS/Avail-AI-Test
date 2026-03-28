"""FastAPI application — all routes."""

from .logging_config import setup_logging

setup_logging()  # Must run before any other module logs

import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from loguru import logger
from sqlalchemy.orm import Session
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import APP_VERSION, settings
from .database import get_db

# Schema managed by Alembic migrations — see alembic/ directory
# To apply:  alembic upgrade head
# To generate: alembic revision --autogenerate -m "description"
# Existing DB: alembic stamp head  (mark as current without running DDL)


@asynccontextmanager
async def lifespan(app):
    """App startup/shutdown — launches background scheduler."""
    from .startup import run_startup_migrations

    # S1: Fail-fast on default secret key (skip in test mode)
    if not os.environ.get("TESTING"):
        if settings.secret_key == "change-me-in-production":
            raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set. See .env.example for required variables.")

    # S2: Warn about missing critical env vars (don't crash — vendor keys are optional)
    if not os.environ.get("TESTING"):
        missing = []
        if not settings.azure_client_id:
            missing.append("AZURE_CLIENT_ID")
        if not settings.azure_client_secret:
            missing.append("AZURE_CLIENT_SECRET")
        if not settings.azure_tenant_id:
            missing.append("AZURE_TENANT_ID")
        if missing:
            logger.warning("Missing env vars (some features disabled): %s", ", ".join(missing))

    # Sentry error tracking (conditional on DSN being set)
    if settings.sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.httpx import HttpxIntegration
        from sentry_sdk.integrations.loguru import LoguruIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        def _sentry_before_send(event, _hint):
            """Scrub sensitive data from Sentry events."""
            _SENSITIVE_HEADERS = {
                "authorization",
                "cookie",
                "x-api-key",
                "anthropic-api-key",
                "session",
            }
            _SENSITIVE_VARS = {
                "api_key",
                "apikey",
                "api_secret",
                "password",
                "secret",
                "token",
                "dsn",
                "database_url",
            }
            if "request" in event:
                req = event["request"]
                hdrs = req.get("headers", {})
                if isinstance(hdrs, dict):
                    for k in list(hdrs):
                        if k.lower() in _SENSITIVE_HEADERS:
                            hdrs[k] = "[Filtered]"
                qs = req.get("query_string", "")
                if isinstance(qs, str) and "key" in qs.lower():
                    req["query_string"] = "[Filtered]"
            for frame in (event.get("exception", {}) or {}).get("values", []) or []:
                for sf in (frame.get("stacktrace", {}) or {}).get("frames", []) or []:
                    for k in list((sf.get("vars") or {})):
                        if any(s in k.lower() for s in _SENSITIVE_VARS):
                            sf["vars"][k] = "[Filtered]"
            return event

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry_profiles_sample_rate,
            environment="production" if "https" in settings.app_url else "development",
            release=APP_VERSION,
            integrations=[
                FastApiIntegration(),
                SqlalchemyIntegration(),
                LoguruIntegration(level=logging.WARNING, event_level=logging.ERROR),
                HttpxIntegration(),
            ],
            before_send=_sentry_before_send,
        )
        logger.info("Sentry initialized (logging + tracing + AI monitoring)")

    run_startup_migrations()

    _is_testing = os.environ.get("TESTING") == "1"

    if not _is_testing:
        from .startup import seed_api_sources

        seed_api_sources()
        from .connector_status import log_connector_status

        _connector_status = log_connector_status()
        app.state.connector_status = _connector_status

        from .scheduler import configure_scheduler, scheduler

        configure_scheduler()
        scheduler.start()
        logger.info("APScheduler started")

    yield

    if not _is_testing:
        logger.info("Shutting down scheduler (waiting for running jobs)...")
        scheduler.shutdown(wait=True)
        from .http_client import close_clients

        await close_clients()
        logger.info("Shutdown complete")


OPENAPI_TAGS = [
    {"name": "auth", "description": "Azure AD OAuth2 login, logout, and token management"},
    {"name": "requisitions", "description": "Requisitions, requirements, search, and sightings"},
    {"name": "vendors", "description": "Vendor cards, contacts, reviews, and material cards"},
    {"name": "rfq", "description": "RFQ email workflows — send, track, and parse responses"},
    {"name": "crm", "description": "Companies, sites, contacts, offers, quotes, and buy plans"},
    {"name": "sources", "description": "API source configuration and connector status"},
    {"name": "ai", "description": "AI chat, response re-parsing, and prospect contacts"},
    {"name": "v13", "description": "Activity logging, webhooks, ownership, and sales dashboard"},
    {"name": "proactive", "description": "Proactive offer matching, sending, and scorecard"},
    {"name": "performance", "description": "Vendor scorecards and buyer leaderboard"},
    {"name": "admin", "description": "User management, system config, and diagnostics"},
    {"name": "emails", "description": "Email mining, inbox scan, and thread views"},
    {"name": "enrichment", "description": "Contact and company enrichment queue and backfills"},
    {"name": "documents", "description": "Document generation and templates"},
]

app = FastAPI(
    title="AVAIL — Electronic Component Sourcing",
    description="Electronic component sourcing engine with vendor intelligence, RFQ automation, and CRM.",
    version=APP_VERSION,
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

# Rate limiting (slowapi)
from .rate_limit import limiter

app.state.limiter = limiter
if settings.rate_limit_enabled:
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Return structured JSON for all HTTP errors."""
    req_id = getattr(request.state, "request_id", "unknown")
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail) if exc.detail else "Error"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": detail,
            "status_code": exc.status_code,
            "request_id": req_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return structured JSON for request validation errors."""
    req_id = getattr(request.state, "request_id", "unknown")
    # Sanitize errors: ctx.error may contain non-serializable ValueError objects
    errors = []
    for err in exc.errors():
        clean = {k: v for k, v in err.items() if k != "ctx"}
        if "ctx" in err and isinstance(err["ctx"], dict):
            clean["ctx"] = {k: str(v) if isinstance(v, Exception) else v for k, v in err["ctx"].items()}
        errors.append(clean)
    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation error",
            "status_code": 422,
            "request_id": req_id,
            "detail": errors,
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch unhandled exceptions — return structured JSON, log with context."""
    req_id = getattr(request.state, "request_id", "unknown")
    logger.error(
        "Unhandled {exc_type}: {exc_msg}",
        exc_type=type(exc).__name__,
        exc_msg=str(exc)[:500],
        method=request.method,
        path=request.url.path,
        request_id=req_id,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "status_code": 500,
            "type": type(exc).__name__,
            "request_id": req_id,
        },
    )


app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.app_url.startswith("https"),
    same_site="lax",
    max_age=86400,
)

# GZip responses ≥ 500 bytes — big wins on JSON-heavy API payloads
app.add_middleware(GZipMiddleware, minimum_size=500)

# CSRF protection (double-submit cookie) — disabled in test mode
if not os.environ.get("TESTING"):
    import re

    from starlette_csrf import CSRFMiddleware

    app.add_middleware(
        CSRFMiddleware,
        secret=settings.secret_key,
        sensitive_cookies={"session"},  # Only enforce CSRF when session cookie is present
        exempt_urls=[
            re.compile(r"/auth/callback$"),
            re.compile(r"/auth/login$"),
            re.compile(r"/health$"),
            re.compile(r"/metrics$"),
            re.compile(r"/api/buy-plans/token/.*"),  # external approval links
            re.compile(r"/v2/partials/requisitions/import-.*"),  # multipart file upload
            re.compile(r"/v2/partials/customers/lookup"),  # AI company lookup (read-only)
        ],
    )

_static_dir = "app/static/dist" if os.path.isdir("app/static/dist") else "app/static"
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# Prometheus metrics
from prometheus_fastapi_instrumentator import Instrumentator


async def _metrics_auth(x_metrics_token: str = Header(default="")) -> None:
    """Require a valid metrics token for /metrics access.

    Uses hmac.compare_digest() for constant-time comparison to prevent timing attacks.
    Returns 403 if token is missing, empty, or wrong.
    """
    token = settings.metrics_token
    if not token or not hmac.compare_digest(x_metrics_token, token):
        raise HTTPException(status_code=403, detail="Forbidden")


Instrumentator(excluded_handlers=["/metrics", "/health", "/static/*"]).instrument(app).expose(
    app, endpoint="/metrics", include_in_schema=False, dependencies=[Depends(_metrics_auth)]
)

# Secret key validation moved to lifespan (fail-fast)


# L0: CSP middleware — restrict script/style sources
@app.middleware("http")
async def csp_middleware(request: Request, call_next):
    """Add Content-Security-Policy header.

    'unsafe-inline' — needed for inline event handlers and <style> tags. 'unsafe-eval' —
    required by Alpine.js which uses new Function() to evaluate x-data, x-show, @click
    and other directive expressions.
    """
    response = await call_next(request)
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdnjs.cloudflare.com https://unpkg.com https://cdn.jsdelivr.net https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'"
    )
    response.headers["Content-Security-Policy"] = csp
    return response


# L1: Request/response middleware — request ID, timing, structured logging
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    import time

    from loguru import logger

    req_id = str(uuid.uuid4())[:8]
    request.state.request_id = req_id
    start = time.perf_counter()

    with logger.contextualize(request_id=req_id):
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.exception(
                "Unhandled exception",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["X-Request-ID"] = req_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if settings.app_url.startswith("https"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        path = request.url.path

        # Cache-Control for static assets (hashed filenames from Vite get long cache)
        if path.startswith("/static/"):
            if "/assets/" in path:  # Vite-hashed filenames — immutable
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "public, max-age=3600"

        # Skip noisy paths (static files, health checks)
        if not (path.startswith("/static") or path == "/health"):
            logger.info(
                "{method} {path} → {status} ({dur}ms)",
                method=request.method,
                path=path,
                status=response.status_code,
                dur=duration_ms,
            )

    return response


# L2: API versioning — accept /api/v1/... and rewrite to /api/... internally.
# This lets the frontend migrate to versioned paths without touching any router decorators.
# When all callers use /api/v1/, we can flip canonical direction.
@app.middleware("http")
async def api_version_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/v1/"):
        scope = request.scope
        scope["path"] = "/api/" + path[8:]  # strip "/api/v1/" → "/api/"
        scope["raw_path"] = scope["path"].encode("utf-8")
    response = await call_next(request)
    response.headers["X-API-Version"] = "v1"
    return response


@app.get("/sw.js", include_in_schema=False)
async def root_sw():
    """Serve self-destruct service worker at root scope to kill any old SW."""
    from fastapi.responses import Response

    body = (
        "self.addEventListener('install',function(){self.skipWaiting()});\n"
        "self.addEventListener('activate',function(e){e.waitUntil("
        "caches.keys().then(function(n){return Promise.all(n.map(function(k){return caches.delete(k)}))})"
        ".then(function(){return self.registration.unregister()})"
        ".then(function(){return self.clients.claim()}))});\n"
    )
    return Response(
        content=body,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, must-revalidate", "Service-Worker-Allowed": "/"},
    )


@app.get("/health")
async def health(
    request: Request,
    db: Session = Depends(get_db),
    x_metrics_token: str = Header(default=""),
):
    from sqlalchemy import text

    from .cache.intel_cache import _get_redis

    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    # "degraded" only when a required service is actively failing
    degraded = not db_ok
    status = "degraded" if degraded else "ok"

    # Public response: minimal — just status, db check, and build commit
    payload: dict = {
        "status": status,
        "db": "ok" if db_ok else "error",
        "build_commit": os.environ.get("BUILD_COMMIT", "unknown"),
    }

    # Detailed info only for authenticated monitoring (same token as /metrics)
    _is_authed = bool(
        settings.metrics_token and x_metrics_token and hmac.compare_digest(x_metrics_token, settings.metrics_token)
    )
    if _is_authed:
        from . import scheduler as sched_mod
        from .services.health_service import check_backup_freshness

        redis_status = "off"
        try:
            r = _get_redis()
            if r is not None:
                redis_status = "ok" if r.ping() else "error"
        except Exception:
            redis_status = "error"

        scheduler_running = getattr(sched_mod.scheduler, "running", False)
        scheduler_status = "ok" if scheduler_running else "off"

        connector_status = getattr(request.app.state, "connector_status", {})
        connectors_enabled = sum(1 for v in connector_status.values() if v)

        backup_status = check_backup_freshness()

        degraded = not db_ok or redis_status == "error" or scheduler_status == "error"
        payload["status"] = "degraded" if degraded else "ok"
        payload["version"] = APP_VERSION
        payload["redis"] = redis_status
        payload["scheduler"] = scheduler_status
        payload["connectors_enabled"] = connectors_enabled
        payload["backup"] = backup_status

    from fastapi.responses import JSONResponse

    return JSONResponse(
        content=payload,
        status_code=200 if payload["status"] == "ok" else 503,
    )


# ── Router Registration ──────────────────────────────────────────────────
# Imports grouped by domain, then registered.

from .routers.activity import router as activity_router
from .routers.admin import router as admin_router
from .routers.ai import router as ai_router
from .routers.auth import router as auth_router
from .routers.command_center import router as command_center_router
from .routers.crm import router as crm_router
from .routers.documents import router as documents_router
from .routers.emails import router as emails_router
from .routers.error_reports import router as error_reports_router
from .routers.events import router as events_router
from .routers.excess import router as excess_router
from .routers.htmx_views import router as htmx_views_router
from .routers.ics_admin import router as ics_admin_router
from .routers.knowledge import insights_router as knowledge_insights_router
from .routers.knowledge import router as knowledge_router
from .routers.knowledge import sprinkles_router as knowledge_sprinkles_router
from .routers.materials import router as materials_router
from .routers.nc_admin import router as nc_admin_router
from .routers.outreach import router as outreach_router
from .routers.proactive import router as proactive_router
from .routers.prospect_pool import router as prospect_pool_router
from .routers.prospect_suggested import router as prospect_suggested_router
from .routers.quote_builder import router as quote_builder_router
from .routers.requisitions import router as reqs_router
from .routers.requisitions2 import router as requisitions2_router
from .routers.rfq import router as rfq_router
from .routers.sightings import router as sightings_router
from .routers.sources import router as sources_router
from .routers.strategic import router as strategic_router
from .routers.tagging_admin import router as tagging_admin_router
from .routers.tags import router as tags_router
from .routers.task import my_tasks_router
from .routers.task import router as task_router
from .routers.v13_features import router as v13_router
from .routers.vendor_analytics import router as vendor_analytics_router
from .routers.vendor_contacts import router as vendor_contacts_router
from .routers.vendor_inquiry import router as vendor_inquiry_router
from .routers.vendors_crud import router as vendors_crud_router

# Core routers (always active)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(ai_router)
app.include_router(activity_router)
app.include_router(command_center_router)
app.include_router(crm_router)
app.include_router(documents_router)
app.include_router(emails_router)
app.include_router(events_router)
app.include_router(excess_router)
app.include_router(error_reports_router)
app.include_router(ics_admin_router)
app.include_router(knowledge_router)
app.include_router(knowledge_insights_router)
app.include_router(knowledge_sprinkles_router)
app.include_router(materials_router)
app.include_router(nc_admin_router)
app.include_router(outreach_router)
app.include_router(proactive_router)
app.include_router(prospect_pool_router)
app.include_router(prospect_suggested_router)
app.include_router(reqs_router)
app.include_router(requisitions2_router)
app.include_router(rfq_router)
app.include_router(sightings_router)
app.include_router(sources_router)
app.include_router(strategic_router)
app.include_router(tags_router)
app.include_router(tagging_admin_router)
app.include_router(task_router)
app.include_router(my_tasks_router)
app.include_router(v13_router)
app.include_router(vendor_analytics_router)
app.include_router(vendor_contacts_router)
app.include_router(vendor_inquiry_router)
app.include_router(vendors_crud_router)
app.include_router(quote_builder_router)
app.include_router(htmx_views_router)

"""FastAPI application — all routes."""

from .logging_config import setup_logging

setup_logging()  # Must run before any other module logs

import hmac
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from loguru import logger
from sqlalchemy.orm import Session
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .audit_listeners import register_audit_listeners
from .config import APP_VERSION, settings
from .database import get_db

# Register CRM audit-trail event listeners (before_insert / before_update).
# Must run at import time, before any ORM session is used, so listeners
# are in place for the first request.
register_audit_listeners()

# Schema managed by Alembic migrations — see alembic/ directory
# To apply:  alembic upgrade head
# To generate: alembic revision --autogenerate -m "description"
# Existing DB: alembic stamp head  (mark as current without running DDL)


@asynccontextmanager
async def lifespan(app):
    """App startup/shutdown — launches background scheduler."""
    from .startup import ensure_avatar_storage, ensure_screenshot_storage, run_startup_migrations

    if not os.environ.get("TESTING"):
        # S1: Fail-fast on default secret key (skip in test mode)
        if settings.secret_key == "change-me-in-production":
            raise RuntimeError("SESSION_SECRET or SECRET_KEY must be set. See .env.example for required variables.")

        # S1b: Fail-fast if the trouble-ticket screenshot storage dir isn't
        # writable by this process (TT-0002) — surfaces a misconfigured/root-owned
        # uploads volume at boot instead of silently dropping screenshots later.
        ensure_screenshot_storage()
        # Same guard for the parallel profile-avatar subdir on the uploads volume.
        ensure_avatar_storage()

        # S2: Warn about missing critical env vars (don't crash — vendor keys are optional)
        missing = []
        if not settings.azure_client_id:
            missing.append("AZURE_CLIENT_ID")
        if not settings.azure_client_secret:
            missing.append("AZURE_CLIENT_SECRET")
        if not settings.azure_tenant_id:
            missing.append("AZURE_TENANT_ID")
        if missing:
            logger.warning("Missing env vars (some features disabled): {}", ", ".join(missing))

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
        from .startup import seed_api_sources, seed_browser_workers

        seed_api_sources()
        seed_browser_workers()
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

        from .database import engine

        engine.dispose()
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


class AuditUserMiddleware:
    """ASGI middleware that populates the request-scoped current_user_id contextvar.

    Must be registered via add_middleware BEFORE SessionMiddleware is registered.
    add_middleware() uses insert(0, ...), so adding Audit first and Session second
    results in user_middleware = [Session, Audit].  build_middleware_stack() then
    reverses and wraps: Audit is innermost, Session is outer — meaning Session runs
    FIRST on the inbound path, populates scope["session"], and Audit reads it.

    Inbound execution order: ... → Session (decodes cookie) → Audit (reads session) → router

    Reads user_id from scope["session"] (same source as require_user) and sets the
    contextvar for the duration of the request.  Resets in a finally block to prevent
    cross-request leaks.  Background jobs have no request → contextvar stays None →
    audit columns stay NULL (correct behaviour).
    """

    def __init__(self, app_inner):  # noqa: ANN001
        self._app = app_inner

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] == "http":
            from .request_context import current_user_id_var

            uid = (scope.get("session") or {}).get("user_id")
            token = current_user_id_var.set(uid)
            try:
                await self._app(scope, receive, send)
            finally:
                current_user_id_var.reset(token)
        else:
            await self._app(scope, receive, send)


class ModuleAccessMiddleware:
    """ASGI chokepoint enforcing per-user MODULE access on module-exclusive HTMX sub-
    partials.

    Each module's *entry* partial is already gated by ``require_access(<key>)``; this
    closes the remaining gap where a user with a module revoked could still READ that
    module's *sub*-partials by direct URL (those sub-partials carry only
    ``require_user``). One chokepoint beats per-sub-partial gates.

    Registration mirrors AuditUserMiddleware: it must be added via add_middleware
    BEFORE SessionMiddleware so that after LIFO ordering Session is outer and this is
    inner — Session decodes the cookie into scope["session"] first, then this reads it.

    Inbound order: ... → Session (decodes cookie) → ModuleAccess (reads session) → router

    Safety: only the EMPIRICALLY module-exclusive prefixes are guarded
    (app.access_paths.module_key_for_path). SHARED partials — CRM data
    (customers/contacts/vendors), the shared module entry-partials
    (parts/sightings/materials/search/buy-plans), capability/global partials, and
    global search — resolve to None and pass through untouched. The decision is
    computed FIRST and a DB session is opened ONLY when a guarded prefix matches, so
    the overwhelming majority of requests pay nothing. Logged-out requests (no
    session user_id — covers x-agent-key auth and test DI overrides too) pass through;
    the route's own deps still enforce auth. Admins are never blocked (user_has_access
    returns True for admin).
    """

    # Methods that can render/mutate a fragment. HEAD/OPTIONS are harmless and skipped
    # so preflight/probe traffic never opens a DB session.
    _GUARDED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, app_inner):  # noqa: ANN001
        self._app = app_inner

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http" or scope.get("method") not in self._GUARDED_METHODS:
            await self._app(scope, receive, send)
            return

        # Cheap path decision FIRST — no DB unless a guarded prefix matches.
        from .access_paths import module_key_for_path

        key = module_key_for_path(scope.get("path", ""))
        if key is None:
            await self._app(scope, receive, send)
            return

        user_id = (scope.get("session") or {}).get("user_id")
        if not user_id:
            # Logged out / agent key / test override — let the route's deps decide.
            await self._app(scope, receive, send)
            return

        from .database import SessionLocal
        from .dependencies import user_has_access
        from .models import User

        db = SessionLocal()
        try:
            user = db.get(User, user_id)
            allowed = user is not None and user_has_access(user, key, db)
        finally:
            db.close()

        if allowed:
            await self._app(scope, receive, send)
            return

        # HTMX fragment request — a plain 403 body is all the client shows.
        from starlette.responses import PlainTextResponse

        await PlainTextResponse("Module access denied", status_code=403)(scope, receive, send)


# Register AuditUserMiddleware and ModuleAccessMiddleware BEFORE SessionMiddleware so
# that after LIFO ordering and reversal, Session is outermost — it populates
# scope["session"] first, then both inner middlewares read it. ModuleAccess is added
# after Audit (so it wraps inside Audit), which is irrelevant to correctness: both
# only read the session Session already decoded.
app.add_middleware(ModuleAccessMiddleware)
app.add_middleware(AuditUserMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=settings.app_url.startswith("https"),
    same_site="lax",
    max_age=86400,
)

# GZip responses ≥ 500 bytes — big wins on JSON-heavy API payloads
app.add_middleware(GZipMiddleware, minimum_size=500)

# CSRF double-submit-cookie exemptions. Matched with re.Pattern.match against
# request.url.path (starlette_csrf ``_url_is_exempt``), so anchor the end with ``$``
# to stop a prefix from over-matching a sibling route. Only genuinely
# CSRF-incompatible or unauthenticated endpoints belong here; every state-changing
# authenticated route must stay under x-csrftoken enforcement. Exposed as a
# module-level constant so the exempt set is unit-testable (see tests).
CSRF_EXEMPT_URLS = [
    re.compile(r"/auth/callback$"),
    re.compile(r"/auth/login$"),
    re.compile(r"/health$"),
    re.compile(r"/metrics$"),
    # Import PREVIEW only: import-parse is a multipart upload (a plain browser form
    # post can't add the x-csrftoken header) and import-form is a GET. import-save
    # (POST that CREATES requisition + requirement rows) is deliberately EXCLUDED so
    # it stays under standard CSRF enforcement like every other state-changing route.
    re.compile(r"/v2/partials/requisitions/import-(form|parse)$"),
    re.compile(r"/v2/partials/customers/lookup"),  # AI company lookup (read-only)
    re.compile(r"/api/webhooks/graph$"),  # Microsoft Graph mail webhook
    re.compile(r"/api/webhooks/teams$"),  # Microsoft Graph Teams webhook
    re.compile(r"/api/webhooks/acs$"),  # Azure Communication Services webhook
]

# CSRF protection (double-submit cookie) — disabled in test mode
if not os.environ.get("TESTING"):
    from starlette_csrf import CSRFMiddleware

    app.add_middleware(
        CSRFMiddleware,
        secret=settings.secret_key,
        sensitive_cookies={"session"},  # Only enforce CSRF when session cookie is present
        exempt_urls=CSRF_EXEMPT_URLS,
    )


# Serve /static from the built dist FIRST, then the source tree as a fallback.
# Public images + bundled js land only in dist (Vite copies publicDir → dist root),
# while unbundled source CSS/JS live only in source — no single dir has both, so
# toggling between them always 404'd one set. Falling back across both resolves
# everything app-direct and matches what Caddy serves in prod (dist) without
# dev-vs-prod divergence.
class _FallbackStaticFiles(StaticFiles):
    """StaticFiles that looks a path up across several directories in order."""

    def __init__(self, directories: list[str]) -> None:
        self._directories = [d for d in directories if os.path.isdir(d)] or ["app/static"]
        super().__init__(directory=self._directories[0], check_dir=False)

    def lookup_path(self, path: str):
        for directory in self._directories:
            full = os.path.realpath(os.path.join(directory, path))
            base = os.path.realpath(directory)
            # Block traversal outside the served directory.
            if os.path.commonpath([full, base]) != base:
                continue
            try:
                return full, os.stat(full)
            except (FileNotFoundError, NotADirectoryError):
                continue
        return "", None


# /static/assets stays a dedicated mount (most specific) for the hashed Vite bundles.
if os.path.isdir("app/static/dist/assets"):
    app.mount(
        "/static/assets",
        StaticFiles(directory="app/static/dist/assets"),
        name="static-assets",
    )
app.mount("/static", _FallbackStaticFiles(["app/static/dist", "app/static"]), name="static")

# Prometheus metrics
from fastapi import Response

from app.prometheus_metrics import PrometheusMiddleware, render_metrics


async def _metrics_auth(x_metrics_token: str = Header(default="")) -> None:
    """Require a valid metrics token for /metrics access.

    Uses hmac.compare_digest() for constant-time comparison to prevent timing attacks.
    Returns 403 if token is missing, empty, or wrong.
    """
    token = settings.metrics_token
    if not token or not hmac.compare_digest(x_metrics_token, token):
        raise HTTPException(status_code=403, detail="Forbidden")


# PrometheusMiddleware wraps outside the add_middleware() cluster above (Session, GZip,
# CSRF): Starlette inserts each add_middleware() at the front of the stack, so registering
# it after them makes it wrap around them. It is NOT the outermost middleware — the
# @app.middleware("http") handlers defined below (csp, request_id, api_version) register
# later and so wrap outside it. Its wall-clock timing therefore covers routing plus the
# Session/GZip/CSRF cluster, but not those three http handlers. Keep it here — after the
# add_middleware() cluster, before the http handlers — to preserve that timing scope and
# the metrics contract.
app.add_middleware(PrometheusMiddleware)


@app.get("/metrics", include_in_schema=False, dependencies=[Depends(_metrics_auth)])
async def metrics_endpoint() -> Response:
    body, content_type = render_metrics()
    return Response(content=body, media_type=content_type)


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
        # Every HTML response — full-page shell AND HTMX partials (/v2/partials/*) — is
        # made non-cacheable so a new deploy's markup is fetched fresh instead of a
        # heuristically-cached stale fragment. Without this, browsers cache partial GETs
        # and in-app HTMX navigation keeps swapping in stale UI until a hard-refresh.
        # Guard is the response content-type ONLY (starts with "text/html"): that naturally
        # excludes JSON, content-hashed /static assets (handled above), text/event-stream
        # SSE streams, and file downloads (PDF/CSV/image Content-Disposition responses),
        # and we never read the response body — so streaming responses stay intact. Set
        # HERE (outermost middleware) because header sets on the TemplateResponse itself
        # are dropped by inner response processing before reaching the client.
        elif (response.headers.get("content-type") or "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"

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

    redis_ok = True
    try:
        r_pub = _get_redis()
        if r_pub:
            r_pub.ping()
    except Exception:
        redis_ok = False

    # "degraded" only when a required service is actively failing
    degraded = not db_ok or not redis_ok
    status = "degraded" if degraded else "ok"

    # Public response: minimal — just status, db check, redis, and build commit
    payload: dict = {
        "status": status,
        "db": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
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

    return JSONResponse(
        content=payload,
        status_code=200 if payload["status"] == "ok" else 503,
    )


# ── Router Registration ──────────────────────────────────────────────────
# Imports grouped by domain, then registered.

from .routers.activity import router as activity_router
from .routers.admin import router as admin_router
from .routers.ai import router as ai_router
from .routers.alerts import router as alerts_router
from .routers.approvals import router as approvals_router
from .routers.attachments_extra import router as attachments_extra_router
from .routers.auth import router as auth_router
from .routers.avatars import router as avatars_router
from .routers.clay_oauth import router as clay_oauth_router
from .routers.crm import router as crm_router
from .routers.documents import router as documents_router
from .routers.error_reports import router as error_reports_router
from .routers.events import router as events_router
from .routers.htmx.archive import router as htmx_archive_router
from .routers.htmx.buy_plans import router as htmx_buy_plans_router
from .routers.htmx.companies import router as htmx_companies_router
from .routers.htmx.materials import router as htmx_materials_router
from .routers.htmx.offers import router as htmx_offers_router
from .routers.htmx.parts import router as htmx_parts_router
from .routers.htmx.proactive import router as htmx_proactive_router
from .routers.htmx.prospecting import router as htmx_prospecting_router
from .routers.htmx.quotes import router as htmx_quotes_router
from .routers.htmx.requisitions import router as htmx_requisitions_router
from .routers.htmx.settings import router as htmx_settings_router
from .routers.htmx.sourcing import router as htmx_sourcing_router
from .routers.htmx.vendors import router as htmx_vendors_router
from .routers.htmx_views import router as htmx_views_router
from .routers.materials import router as materials_router
from .routers.part_dossier import router as part_dossier_router
from .routers.prepayments import router as prepayments_router
from .routers.proactive import router as proactive_router
from .routers.quality_plans import router as quality_plans_router
from .routers.quote_builder import router as quote_builder_router
from .routers.requisitions import router as reqs_router
from .routers.requisitions2 import router as requisitions2_router
from .routers.resell import router as resell_router
from .routers.sightings import router as sightings_router
from .routers.sources import router as sources_router
from .routers.tags import router as tags_router
from .routers.v13_features import router as v13_router
from .routers.vendor_contacts import router as vendor_contacts_router
from .routers.vendors_crud import router as vendors_crud_router

# Core routers (always active)
app.include_router(attachments_extra_router)
app.include_router(auth_router)
app.include_router(avatars_router)
app.include_router(clay_oauth_router)
app.include_router(admin_router)
app.include_router(ai_router)
app.include_router(alerts_router)
app.include_router(activity_router)
app.include_router(crm_router)
app.include_router(documents_router)
app.include_router(events_router)
app.include_router(error_reports_router)
app.include_router(materials_router)
app.include_router(part_dossier_router)
app.include_router(proactive_router)
app.include_router(reqs_router)
app.include_router(requisitions2_router)
app.include_router(sightings_router)
app.include_router(sources_router)
app.include_router(tags_router)
app.include_router(resell_router)
app.include_router(v13_router)
app.include_router(approvals_router)
app.include_router(prepayments_router)
app.include_router(quality_plans_router)
app.include_router(vendor_contacts_router)
app.include_router(vendors_crud_router)
app.include_router(quote_builder_router)
app.include_router(htmx_views_router)
app.include_router(htmx_requisitions_router)
app.include_router(htmx_vendors_router)
app.include_router(htmx_companies_router)
app.include_router(htmx_buy_plans_router)
app.include_router(htmx_offers_router)
app.include_router(htmx_sourcing_router)
app.include_router(htmx_quotes_router)
app.include_router(htmx_prospecting_router)
app.include_router(htmx_settings_router)
app.include_router(htmx_materials_router)
app.include_router(htmx_proactive_router)
app.include_router(htmx_parts_router)
app.include_router(htmx_archive_router)

"""FastAPI application — all routes."""

from .logging_config import setup_logging

setup_logging()  # Must run before any other module logs

import os
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .config import APP_VERSION, settings
from .database import get_db
from .models import (
    ApiSource,
)

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
            raise RuntimeError(
                "SESSION_SECRET or SECRET_KEY must be set. "
                "See .env.example for required variables."
            )

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
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry_profiles_sample_rate,
            environment="production" if "https" in settings.app_url else "development",
            release=APP_VERSION,
        )
        logger.info("Sentry initialized (DSN configured)")

    run_startup_migrations()
    _seed_api_sources()
    from .connector_status import log_connector_status
    _connector_status = log_connector_status()
    app.state.connector_status = _connector_status

    from .scheduler import configure_scheduler, scheduler

    configure_scheduler()
    scheduler.start()
    logger.info("APScheduler started")
    yield
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
    title="AVAIL — Opportunity Management",
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
    return JSONResponse(
        status_code=422,
        content={
            "error": "Validation error",
            "status_code": 422,
            "request_id": req_id,
            "detail": exc.errors(),
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

# CSRF protection (double-submit cookie) — disabled in test mode
if not os.environ.get("TESTING"):
    import re

    from starlette_csrf import CSRFMiddleware

    app.add_middleware(
        CSRFMiddleware,
        secret=settings.secret_key,
        sensitive_cookies={"session"},  # Only enforce CSRF when session cookie is present
        exempt_urls=[
            re.compile(r"/auth/.*"),
            re.compile(r"/health"),
            re.compile(r"/metrics"),
            re.compile(r"/api/buy-plans/token/.*"),  # external approval links
        ],
    )

_static_dir = "app/static/dist" if os.path.isdir("app/static/dist") else "app/static"
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
templates = Jinja2Templates(directory="app/templates")

# Prometheus metrics
from prometheus_fastapi_instrumentator import Instrumentator

Instrumentator(excluded_handlers=["/metrics", "/health", "/static/*"]).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

# Secret key validation moved to lifespan (fail-fast)


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

        # Skip noisy paths (static files, health checks)
        path = request.url.path
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


# ── Health Check ──────────────────────────────────────────────────────
BACKUP_TIMESTAMP_FILE = "/app/uploads/.last_backup"
BACKUP_MAX_AGE_HOURS = 25  # Backups older than this are "stale"


def _check_backup_freshness() -> str:
    """Check if the last backup timestamp is recent enough.

    Returns "ok", "stale", or "unknown".
    """
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    ts_path = Path(BACKUP_TIMESTAMP_FILE)
    if not ts_path.exists():
        return "unknown"

    try:
        raw = ts_path.read_text().strip()
        # Parse ISO 8601 timestamp written by backup.sh (date -Iseconds)
        # Handle timezone offset formats: +00:00, +0000, Z
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        backup_time = datetime.fromisoformat(raw)
        # If naive (no timezone), assume UTC
        if backup_time.tzinfo is None:
            backup_time = backup_time.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - backup_time
        if age < timedelta(hours=BACKUP_MAX_AGE_HOURS):
            return "ok"
        return "stale"
    except (ValueError, OSError):
        return "unknown"


@app.get("/health")
async def health(request: Request, db: Session = Depends(get_db)):
    from sqlalchemy import text

    from . import scheduler as sched_mod
    from .cache.intel_cache import _get_redis

    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    redis_status = "off"
    try:
        r = _get_redis()
        if r is not None:
            redis_status = "ok" if r.ping() else "error"
    except Exception:
        redis_status = "error"

    scheduler_running = getattr(sched_mod.scheduler, "running", False)
    scheduler_status = "ok" if scheduler_running else "off"

    # Connector status from startup scan
    connector_status = getattr(request.app.state, "connector_status", {})
    connectors_enabled = sum(1 for v in connector_status.values() if v)

    # Backup freshness (informational — does not affect overall status)
    backup_status = _check_backup_freshness()

    # "degraded" only when a required service is actively failing
    degraded = not db_ok or redis_status == "error" or scheduler_status == "error"
    status = "degraded" if degraded else "ok"
    from fastapi.responses import JSONResponse

    return JSONResponse(
        content={
            "status": status,
            "version": APP_VERSION,
            "db": "ok" if db_ok else "error",
            "redis": redis_status,
            "scheduler": scheduler_status,
            "connectors_enabled": connectors_enabled,
            "backup": backup_status,
        },
        status_code=200 if status == "ok" else 503,
    )


# ── Seed API Sources ─────────────────────────────────────────────────────
def _seed_api_sources():
    """Seed the api_sources table with all known data sources.
    Uses a version hash so it only writes when the source list changes."""
    import hashlib

    from .database import SessionLocal

    db = SessionLocal()
    try:
        SOURCES = [
            # ── LIVE (have connectors built) ──
            {
                "name": "nexar",
                "display_name": "Octopart (Nexar)",
                "category": "api",
                "source_type": "aggregator",
                "description": "GraphQL API — searches across authorized distributors and brokers via Octopart data. Returns seller, price, qty, authorized status.",
                "signup_url": "https://nexar.com/api",
                "env_vars": ["NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"],
                "setup_notes": "Create app at nexar.com → get client_id + client_secret. Free tier: 1000 queries/month.",
            },
            {
                "name": "brokerbin",
                "display_name": "BrokerBin",
                "category": "api",
                "source_type": "broker",
                "description": "REST API v2 — searches independent broker/distributor inventories. Returns company, MPN, qty, price.",
                "signup_url": "https://www.brokerbin.com",
                "env_vars": ["BROKERBIN_API_KEY", "BROKERBIN_API_SECRET"],
                "setup_notes": "Contact BrokerBin sales for API access. Need API key (token) + username.",
            },
            {
                "name": "ebay",
                "display_name": "eBay",
                "category": "api",
                "source_type": "marketplace",
                "description": "Browse API — searches eBay listings for electronic components. Returns seller, price, condition, qty. Good for surplus/used parts.",
                "signup_url": "https://developer.ebay.com",
                "env_vars": ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"],
                "setup_notes": "Create app at developer.ebay.com → get client_id + secret. OAuth client credentials. Need production access approval.",
            },
            {
                "name": "digikey",
                "display_name": "DigiKey",
                "category": "api",
                "source_type": "authorized",
                "description": "Product Search v4 — real-time pricing and inventory from DigiKey's catalog. Authorized distributor.",
                "signup_url": "https://developer.digikey.com",
                "env_vars": ["DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"],
                "setup_notes": "Register at developer.digikey.com → create organization → create app → get OAuth2 credentials. Free tier available.",
            },
            {
                "name": "mouser",
                "display_name": "Mouser",
                "category": "api",
                "source_type": "authorized",
                "description": "Search API v2 — real-time pricing and stock from Mouser's catalog. Authorized distributor.",
                "signup_url": "https://www.mouser.com/api-hub/",
                "env_vars": ["MOUSER_API_KEY"],
                "setup_notes": "Register at mouser.com → go to API Hub → request Search API key. Choose country/language/currency at signup.",
            },
            {
                "name": "oemsecrets",
                "display_name": "OEMSecrets",
                "category": "api",
                "source_type": "aggregator",
                "description": "Meta-aggregator — ONE API call returns pricing from 140+ distributors (DigiKey, Mouser, Arrow, Avnet, Farnell, RS, Future, TME, etc).",
                "signup_url": "https://www.oemsecrets.com/api",
                "env_vars": ["OEMSECRETS_API_KEY"],
                "setup_notes": "Request API access at oemsecrets.com/api. Provides JSON + JavaScript APIs. Covers 40M+ parts.",
            },
            {
                "name": "sourcengine",
                "display_name": "Sourcengine",
                "category": "api",
                "source_type": "aggregator",
                "description": "B2B marketplace API — search MPN across global supplier network with real-time offers.",
                "signup_url": "https://dev.sourcengine.com",
                "env_vars": ["SOURCENGINE_API_KEY"],
                "setup_notes": "Register at dev.sourcengine.com for API access. Bearer token auth.",
            },
            {
                "name": "email_mining",
                "display_name": "Email Intelligence (M365)",
                "category": "email",
                "source_type": "internal",
                "description": "Scans team Outlook/M365 inbox for vendor offers, stock lists, and contact info. Extracts emails, phones, and part numbers from correspondence.",
                "signup_url": "",
                "env_vars": ["EMAIL_MINING_ENABLED"],
                "setup_notes": "Already authenticated via Azure OAuth. Set EMAIL_MINING_ENABLED=true to activate. Uses existing Graph API token.",
            },
            # ── PLATFORM SERVICES ──
            {
                "name": "azure_oauth",
                "display_name": "Azure OAuth (M365)",
                "category": "platform",
                "source_type": "auth",
                "description": "Azure AD OAuth2 — handles Microsoft 365 login, Graph API tokens for email mining, calendar, and Teams integration.",
                "signup_url": "https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps",
                "env_vars": ["AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"],
                "setup_notes": "Azure Portal → App registrations → New registration. Set redirect URI to your app URL + /auth/callback. Grant Mail.Read, User.Read, Calendars.Read permissions.",
            },
            {
                "name": "anthropic_ai",
                "display_name": "Anthropic AI (Claude)",
                "category": "platform",
                "source_type": "ai",
                "description": "Claude API — powers RFQ response parsing, attachment column mapping, AI chat, and intelligent data extraction.",
                "signup_url": "https://console.anthropic.com",
                "env_vars": ["ANTHROPIC_API_KEY"],
                "setup_notes": "Sign up at console.anthropic.com → API Keys → Create Key. Model defaults to claude-sonnet-4-20250514 (configurable via ANTHROPIC_MODEL).",
            },
            {
                "name": "teams_notifications",
                "display_name": "Teams Notifications",
                "category": "platform",
                "source_type": "notification",
                "description": "Microsoft Teams — sends RFQ alerts, vendor response notifications, and system alerts to a Teams channel.",
                "signup_url": "https://teams.microsoft.com",
                "env_vars": ["TEAMS_WEBHOOK_URL", "TEAMS_TEAM_ID", "TEAMS_CHANNEL_ID"],
                "setup_notes": "Teams → Channel → Connectors → Incoming Webhook → copy URL. Team/Channel IDs from Teams admin or Graph API.",
            },
            # ── ENRICHMENT SERVICES ──
            {
                "name": "apollo_enrichment",
                "display_name": "Apollo.io",
                "category": "enrichment",
                "source_type": "enrichment",
                "description": "Contact and company enrichment — finds decision-maker emails, phone numbers, titles, and company firmographics for vendor outreach.",
                "signup_url": "https://www.apollo.io",
                "env_vars": ["APOLLO_API_KEY"],
                "setup_notes": "Sign up at apollo.io → Settings → API Keys → Generate. Free tier: 10K enrichments/month. Used to enrich VendorCard contacts.",
            },
            {
                "name": "clay_enrichment",
                "display_name": "Clay",
                "category": "enrichment",
                "source_type": "enrichment",
                "description": "Data enrichment platform — waterfall enrichment across 75+ providers for contact emails, phones, LinkedIn, and company data.",
                "signup_url": "https://www.clay.com",
                "env_vars": ["CLAY_API_KEY"],
                "setup_notes": "Sign up at clay.com → Settings → API → Generate Key. Credits-based pricing. Excellent for bulk vendor contact enrichment.",
            },
            {
                "name": "explorium_enrichment",
                "display_name": "Explorium",
                "category": "enrichment",
                "source_type": "enrichment",
                "description": "B2B data enrichment — company technographics, intent signals, and contact data for supplier intelligence and lead scoring.",
                "signup_url": "https://www.explorium.ai",
                "env_vars": ["EXPLORIUM_API_KEY"],
                "setup_notes": "Contact Explorium for API access. Enterprise-grade enrichment with intent data and predictive signals.",
            },
            {
                "name": "hunter_enrichment",
                "display_name": "Hunter.io",
                "category": "enrichment",
                "source_type": "enrichment",
                "description": "Email verification and domain contact discovery — finds verified email addresses and contacts at any company domain.",
                "signup_url": "https://hunter.io",
                "env_vars": ["HUNTER_API_KEY"],
                "setup_notes": "Sign up at hunter.io → API → Get API key. Free tier: 25 searches + 50 verifications/month.",
            },
            {
                "name": "rocketreach_enrichment",
                "display_name": "RocketReach",
                "category": "enrichment",
                "source_type": "enrichment",
                "description": "Contact lookup and search — finds emails, phone numbers, and social profiles for decision-makers at target companies.",
                "signup_url": "https://rocketreach.co",
                "env_vars": ["ROCKETREACH_API_KEY"],
                "setup_notes": "Sign up at rocketreach.co → API → Generate Key. Credits-based pricing with free trial.",
            },
            {
                "name": "clearbit_enrichment",
                "display_name": "Clearbit (HubSpot)",
                "category": "enrichment",
                "source_type": "enrichment",
                "description": "Company and person enrichment — firmographic data (industry, size, revenue, tech stack) and person data (name, title, social profiles).",
                "signup_url": "https://clearbit.com",
                "env_vars": ["CLEARBIT_API_KEY"],
                "setup_notes": "Sign up at clearbit.com (now part of HubSpot) → API → Get key. Free tier available for basic enrichment.",
            },
            # ── PENDING (no connector yet — scraping or future API) ──
            {
                "name": "netcomponents",
                "display_name": "NetComponents",
                "category": "scraper",
                "source_type": "broker",
                "description": "60M+ line items from hundreds of suppliers. Non-anonymous — shows vendor name and contact info. No public API.",
                "signup_url": "https://www.netcomponents.com",
                "env_vars": [],
                "setup_notes": "PENDING: Need browser automation (Perplexity/Playwright) to search with your membership credentials. High-value target — shows vendor contacts directly.",
            },
            {
                "name": "icsource",
                "display_name": "IC Source",
                "category": "scraper",
                "source_type": "broker",
                "description": "Broker marketplace with inventory listings and vendor contacts. Membership-based.",
                "signup_url": "https://www.icsource.com",
                "env_vars": [],
                "setup_notes": "PENDING: Need browser automation to search with membership login. Similar value to NetComponents.",
            },
            {
                "name": "thebrokersite",
                "display_name": "The Broker Forum (TBF)",
                "category": "scraper",
                "source_type": "broker",
                "description": "60M+ line items from broker/distributor network. Has XML Search service (possible API). Escrow services available.",
                "signup_url": "https://www.brokerforum.com",
                "env_vars": [],
                "setup_notes": "PENDING: Investigate XML Search — may have API. Otherwise browser automation. 100K+ parts searched daily by members.",
            },
            {
                "name": "findchips",
                "display_name": "FindChips (Supplyframe)",
                "category": "scraper",
                "source_type": "aggregator",
                "description": "Aggregates authorized distributor pricing and inventory. Parametric search, part alerts, and trend data.",
                "signup_url": "https://www.findchips.com",
                "env_vars": [],
                "setup_notes": "PENDING: No public API. Owned by Supplyframe (Siemens). Well-structured pages for scraping.",
            },
            {
                "name": "arrow",
                "display_name": "Arrow Electronics",
                "category": "api",
                "source_type": "authorized",
                "description": "Major authorized distributor ($28B revenue). Has API program for pricing and inventory.",
                "signup_url": "https://developers.arrow.com",
                "env_vars": [],
                "setup_notes": "PENDING: Register at developers.arrow.com for API access. OAuth2 flow. Need to apply for production access.",
            },
            {
                "name": "avnet",
                "display_name": "Avnet",
                "category": "api",
                "source_type": "authorized",
                "description": "Global authorized distributor with API program. Design support and logistics services.",
                "signup_url": "https://www.avnet.com",
                "env_vars": [],
                "setup_notes": "PENDING: Contact Avnet for API partnership. May require business relationship.",
            },
            {
                "name": "tme",
                "display_name": "TME (Transfer Multisort)",
                "category": "api",
                "source_type": "authorized",
                "description": "European authorized distributor with public REST API. 500K+ products.",
                "signup_url": "https://developers.tme.eu",
                "env_vars": [],
                "setup_notes": "PENDING: Register at developers.tme.eu for API token. Well-documented REST API.",
            },
            {
                "name": "lcsc",
                "display_name": "LCSC Electronics",
                "category": "scraper",
                "source_type": "authorized",
                "description": "Chinese distributor with internal JSON API (unofficial). Low prices, wide SMD selection.",
                "signup_url": "https://www.lcsc.com",
                "env_vars": [],
                "setup_notes": "PENDING: No official API. Internal JSON API can be reverse-engineered. No auth required. Use at own risk.",
            },
            {
                "name": "partfuse",
                "display_name": "PartFuse (Unified API)",
                "category": "api",
                "source_type": "aggregator",
                "description": "Unified API on RapidAPI — DigiKey + Mouser + TME in one JSON shape. Simple integration.",
                "signup_url": "https://rapidapi.com/partfuse/api/partfuse",
                "env_vars": [],
                "setup_notes": "PENDING: Sign up on RapidAPI → subscribe to PartFuse. Header-based auth. Good as backup/redundancy for DigiKey+Mouser.",
            },
            {
                "name": "stock_list_import",
                "display_name": "Vendor Stock List Import",
                "category": "manual",
                "source_type": "internal",
                "description": "Buyers upload Excel/CSV stock lists from vendors. Auto-parsed and imported as sightings + vendor card enrichment.",
                "signup_url": "",
                "env_vars": [],
                "setup_notes": "PENDING: Build upload UI. Parse common stock list formats (MPN, Qty, Price, Manufacturer). Dedupe against existing data.",
            },
            # ── PENDING (additional authorized distributors) ──
            {
                "name": "newark",
                "display_name": "Newark / element14 / Farnell",
                "category": "api",
                "source_type": "authorized",
                "description": "Major authorized distributor (part of Avnet). element14 API covers Newark (Americas), Farnell (Europe), element14 (APAC).",
                "signup_url": "https://partner.element14.com/docs",
                "env_vars": [],
                "setup_notes": "PENDING: Register at element14 Partner API portal. REST API with JSON responses. Single key covers all 3 regional brands.",
            },
            {
                "name": "rs_components",
                "display_name": "RS Components",
                "category": "api",
                "source_type": "authorized",
                "description": "Global authorized distributor with Product Search API. 700K+ products across electronics, industrial, and maintenance.",
                "signup_url": "https://developerportal.rs-online.com",
                "env_vars": [],
                "setup_notes": "PENDING: Register at RS Developer Portal. REST API with OAuth2. Covers pricing, stock, and product specs.",
            },
            {
                "name": "future",
                "display_name": "Future Electronics",
                "category": "api",
                "source_type": "authorized",
                "description": "Top 3 global authorized distributor. Has developer API program for inventory and pricing.",
                "signup_url": "https://www.futureelectronics.com",
                "env_vars": [],
                "setup_notes": "PENDING: Contact Future Electronics for API partnership. May require established business account.",
            },
            {
                "name": "rochester",
                "display_name": "Rochester Electronics",
                "category": "api",
                "source_type": "authorized",
                "description": "Specialist in EOL/obsolete and hard-to-find semiconductors. Licensed manufacturer of discontinued ICs. Critical for legacy parts.",
                "signup_url": "https://www.rocelec.com",
                "env_vars": [],
                "setup_notes": "PENDING: Contact Rochester for API access. Essential for obsolete/EOL part sourcing. Also manufactures discontinued parts under license.",
            },
            {
                "name": "verical",
                "display_name": "Verical (Arrow Marketplace)",
                "category": "api",
                "source_type": "marketplace",
                "description": "Arrow Electronics' open marketplace. Connects buyers with verified suppliers beyond Arrow's own stock.",
                "signup_url": "https://www.verical.com",
                "env_vars": [],
                "setup_notes": "PENDING: Check if Verical offers API access through Arrow's developer program. Marketplace model with supplier verification.",
            },
            {
                "name": "heilind",
                "display_name": "Heilind Electronics",
                "category": "api",
                "source_type": "authorized",
                "description": "Authorized distributor specializing in connectors, relays, sensors, switches, and thermal management.",
                "signup_url": "https://www.heilind.com",
                "env_vars": [],
                "setup_notes": "PENDING: Contact Heilind for API or EDI integration. Strong in interconnect and electromechanical components.",
            },
            {
                "name": "winsource",
                "display_name": "WIN SOURCE",
                "category": "api",
                "source_type": "broker",
                "description": "Global electronic component distributor with 1M+ SKUs. Strong in China-sourced components and hard-to-find parts.",
                "signup_url": "https://www.win-source.net",
                "env_vars": [],
                "setup_notes": "PENDING: Check WIN SOURCE for API access. Large inventory, competitive pricing on Asian-sourced components.",
            },
            {
                "name": "siliconexpert",
                "display_name": "SiliconExpert / Z2Data",
                "category": "api",
                "source_type": "intelligence",
                "description": "Component lifecycle intelligence — EOL risk, cross-references, compliance (RoHS/REACH), market availability trends. Not a seller, but critical for material card enrichment.",
                "signup_url": "https://www.siliconexpert.com/api",
                "env_vars": [],
                "setup_notes": "PENDING: API available for component lifecycle data, cross-refs, and compliance. Enriches material cards with lifecycle risk and alternates.",
            },
            {
                "name": "aliexpress",
                "display_name": "AliExpress",
                "category": "scraper",
                "source_type": "marketplace",
                "description": "Chinese marketplace with electronic components at reference pricing. Useful for cost benchmarking and identifying Chinese suppliers.",
                "signup_url": "https://developers.aliexpress.com",
                "env_vars": [],
                "setup_notes": "PENDING: AliExpress has affiliate API. Useful for reference pricing and Chinese supplier discovery. Not for mission-critical procurement.",
            },
        ]

        # Version hash — skip if source list hasn't changed
        source_hash = hashlib.md5(
            str([(s["name"], s["description"]) for s in SOURCES]).encode(),
            usedforsecurity=False,
        ).hexdigest()[:12]
        existing_map = {s.name: s for s in db.query(ApiSource).all()}

        # Quick check: if all sources exist and count matches, check version
        if len(existing_map) == len(SOURCES) and all(
            s["name"] in existing_map for s in SOURCES
        ):
            # All sources present — skip update (descriptions only change on code update)
            logger.debug(
                f"API sources up to date ({len(SOURCES)} sources, hash={source_hash})"
            )
            return

        # Batch fetch all existing sources (1 query instead of 25+)
        logger.info(f"Seeding API sources ({len(SOURCES)} sources, hash={source_hash})")
        for src in SOURCES:
            existing = existing_map.get(src["name"])
            if existing:
                # Update description/notes but preserve status and stats
                existing.display_name = src["display_name"]
                existing.category = src["category"]
                existing.source_type = src["source_type"]
                existing.description = src["description"]
                existing.signup_url = src["signup_url"]
                existing.env_vars = src["env_vars"]
                existing.setup_notes = src["setup_notes"]
            else:
                # Determine initial status based on env vars
                status = "pending"
                env_vars = src.get("env_vars", [])
                if env_vars:
                    all_set = all(os.getenv(v) for v in env_vars)
                    if all_set:
                        status = "live"
                db.add(ApiSource(status=status, **src))

        db.commit()
    except Exception as e:
        logger.warning(f"API source seed error: {e}")
        db.rollback()
    finally:
        db.close()


# _seed_api_sources() is called from lifespan after startup migrations

# ── Shared dependencies (auth, query helpers) ────────────────────────────


# Auth routes moved to routers/auth.py
from .routers.auth import router as auth_router

app.include_router(auth_router)

from .routers.vendors import router as vendors_router

app.include_router(vendors_router)
from .routers.crm import router as crm_router

app.include_router(crm_router)
from .routers.sources import router as sources_router

app.include_router(sources_router)
from .routers.ai import router as ai_router

app.include_router(ai_router)
from .routers.v13_features import router as v13_router

app.include_router(v13_router)
from .routers.requisitions import router as reqs_router

app.include_router(reqs_router)
from .routers.rfq import router as rfq_router

app.include_router(rfq_router)
from .routers.proactive import router as proactive_router

app.include_router(proactive_router)
from .routers.performance import router as performance_router

app.include_router(performance_router)
from .routers.admin import router as admin_router

app.include_router(admin_router)
from .routers.emails import router as emails_router

app.include_router(emails_router)
from .routers.enrichment import router as enrichment_router

app.include_router(enrichment_router)
from .routers.documents import router as documents_router

app.include_router(documents_router)
from .routers.error_reports import router as error_reports_router

app.include_router(error_reports_router)

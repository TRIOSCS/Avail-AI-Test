"""FastAPI application — all routes."""
from .logging_config import setup_logging
setup_logging()  # Must run before any other module logs

import json, os, re, logging, asyncio, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc, text as sqltext
from starlette.middleware.sessions import SessionMiddleware
import httpx

from .config import settings, APP_VERSION
from .database import engine, get_db
from .models import (
    Base, User, Requisition, Requirement, Sighting, Contact, VendorResponse,
    VendorCard, VendorReview, MaterialCard, MaterialVendorHistory, ApiSource,
    VendorContact, Company, CustomerSite, Offer, Quote,
    SyncLog, InventorySnapshot, ProspectContact, ProcessedMessage,
)
from .search_service import search_requirement, normalize_mpn
from .email_service import send_batch_rfq, log_phone_contact, poll_inbox

log = logging.getLogger(__name__)

# Schema managed by Alembic migrations — see alembic/ directory
# To apply:  alembic upgrade head
# To generate: alembic revision --autogenerate -m "description"
# Existing DB: alembic stamp head  (mark as current without running DDL)

@asynccontextmanager
async def lifespan(app):
    """App startup/shutdown — launches background scheduler."""
    from .startup import run_startup_migrations
    run_startup_migrations()
    from .scheduler import start_scheduler
    task = asyncio.create_task(start_scheduler())
    log.info("Background scheduler launched")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="AVAIL — Opportunity Management", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                   https_only=settings.app_url.startswith("https"),
                   same_site="lax", max_age=86400)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# S2: Warn on default secret key
if settings.secret_key == "change-me-in-production":
    log.critical("⚠️  SESSION_SECRET is using the default value! Set SESSION_SECRET in .env for production.")

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
@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


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
                "name": "nexar", "display_name": "Octopart (Nexar)",
                "category": "api", "source_type": "aggregator",
                "description": "GraphQL API — searches across authorized distributors and brokers via Octopart data. Returns seller, price, qty, authorized status.",
                "signup_url": "https://nexar.com/api",
                "env_vars": ["NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET"],
                "setup_notes": "Create app at nexar.com → get client_id + client_secret. Free tier: 1000 queries/month.",
            },
            {
                "name": "brokerbin", "display_name": "BrokerBin",
                "category": "api", "source_type": "broker",
                "description": "REST API v2 — searches independent broker/distributor inventories. Returns company, MPN, qty, price.",
                "signup_url": "https://www.brokerbin.com",
                "env_vars": ["BROKERBIN_API_KEY", "BROKERBIN_API_SECRET"],
                "setup_notes": "Contact BrokerBin sales for API access. Need API key (token) + username.",
            },
            {
                "name": "ebay", "display_name": "eBay",
                "category": "api", "source_type": "marketplace",
                "description": "Browse API — searches eBay listings for electronic components. Returns seller, price, condition, qty. Good for surplus/used parts.",
                "signup_url": "https://developer.ebay.com",
                "env_vars": ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"],
                "setup_notes": "Create app at developer.ebay.com → get client_id + secret. OAuth client credentials. Need production access approval.",
            },
            {
                "name": "digikey", "display_name": "DigiKey",
                "category": "api", "source_type": "authorized",
                "description": "Product Search v4 — real-time pricing and inventory from DigiKey's catalog. Authorized distributor.",
                "signup_url": "https://developer.digikey.com",
                "env_vars": ["DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"],
                "setup_notes": "Register at developer.digikey.com → create organization → create app → get OAuth2 credentials. Free tier available.",
            },
            {
                "name": "mouser", "display_name": "Mouser",
                "category": "api", "source_type": "authorized",
                "description": "Search API v2 — real-time pricing and stock from Mouser's catalog. Authorized distributor.",
                "signup_url": "https://www.mouser.com/api-hub/",
                "env_vars": ["MOUSER_API_KEY"],
                "setup_notes": "Register at mouser.com → go to API Hub → request Search API key. Choose country/language/currency at signup.",
            },
            {
                "name": "oemsecrets", "display_name": "OEMSecrets",
                "category": "api", "source_type": "aggregator",
                "description": "Meta-aggregator — ONE API call returns pricing from 140+ distributors (DigiKey, Mouser, Arrow, Avnet, Farnell, RS, Future, TME, etc).",
                "signup_url": "https://www.oemsecrets.com/api",
                "env_vars": ["OEMSECRETS_API_KEY"],
                "setup_notes": "Request API access at oemsecrets.com/api. Provides JSON + JavaScript APIs. Covers 40M+ parts.",
            },
            {
                "name": "sourcengine", "display_name": "Sourcengine",
                "category": "api", "source_type": "aggregator",
                "description": "B2B marketplace API — search MPN across global supplier network with real-time offers.",
                "signup_url": "https://dev.sourcengine.com",
                "env_vars": ["SOURCENGINE_API_KEY"],
                "setup_notes": "Register at dev.sourcengine.com for API access. Bearer token auth.",
            },
            {
                "name": "email_mining", "display_name": "Email Intelligence (M365)",
                "category": "email", "source_type": "internal",
                "description": "Scans team Outlook/M365 inbox for vendor offers, stock lists, and contact info. Extracts emails, phones, and part numbers from correspondence.",
                "signup_url": "",
                "env_vars": ["EMAIL_MINING_ENABLED"],
                "setup_notes": "Already authenticated via Azure OAuth. Set EMAIL_MINING_ENABLED=true to activate. Uses existing Graph API token.",
            },

            # ── PENDING (no connector yet — scraping or future API) ──
            {
                "name": "netcomponents", "display_name": "NetComponents",
                "category": "scraper", "source_type": "broker",
                "description": "60M+ line items from hundreds of suppliers. Non-anonymous — shows vendor name and contact info. No public API.",
                "signup_url": "https://www.netcomponents.com",
                "env_vars": [],
                "setup_notes": "PENDING: Need browser automation (Perplexity/Playwright) to search with your membership credentials. High-value target — shows vendor contacts directly.",
            },
            {
                "name": "icsource", "display_name": "IC Source",
                "category": "scraper", "source_type": "broker",
                "description": "Broker marketplace with inventory listings and vendor contacts. Membership-based.",
                "signup_url": "https://www.icsource.com",
                "env_vars": [],
                "setup_notes": "PENDING: Need browser automation to search with membership login. Similar value to NetComponents.",
            },
            {
                "name": "thebrokersite", "display_name": "The Broker Forum (TBF)",
                "category": "scraper", "source_type": "broker",
                "description": "60M+ line items from broker/distributor network. Has XML Search service (possible API). Escrow services available.",
                "signup_url": "https://www.brokerforum.com",
                "env_vars": [],
                "setup_notes": "PENDING: Investigate XML Search — may have API. Otherwise browser automation. 100K+ parts searched daily by members.",
            },
            {
                "name": "findchips", "display_name": "FindChips (Supplyframe)",
                "category": "scraper", "source_type": "aggregator",
                "description": "Aggregates authorized distributor pricing and inventory. Parametric search, part alerts, and trend data.",
                "signup_url": "https://www.findchips.com",
                "env_vars": [],
                "setup_notes": "PENDING: No public API. Owned by Supplyframe (Siemens). Well-structured pages for scraping.",
            },
            {
                "name": "arrow", "display_name": "Arrow Electronics",
                "category": "api", "source_type": "authorized",
                "description": "Major authorized distributor ($28B revenue). Has API program for pricing and inventory.",
                "signup_url": "https://developers.arrow.com",
                "env_vars": [],
                "setup_notes": "PENDING: Register at developers.arrow.com for API access. OAuth2 flow. Need to apply for production access.",
            },
            {
                "name": "avnet", "display_name": "Avnet",
                "category": "api", "source_type": "authorized",
                "description": "Global authorized distributor with API program. Design support and logistics services.",
                "signup_url": "https://www.avnet.com",
                "env_vars": [],
                "setup_notes": "PENDING: Contact Avnet for API partnership. May require business relationship.",
            },
            {
                "name": "tme", "display_name": "TME (Transfer Multisort)",
                "category": "api", "source_type": "authorized",
                "description": "European authorized distributor with public REST API. 500K+ products.",
                "signup_url": "https://developers.tme.eu",
                "env_vars": [],
                "setup_notes": "PENDING: Register at developers.tme.eu for API token. Well-documented REST API.",
            },
            {
                "name": "lcsc", "display_name": "LCSC Electronics",
                "category": "scraper", "source_type": "authorized",
                "description": "Chinese distributor with internal JSON API (unofficial). Low prices, wide SMD selection.",
                "signup_url": "https://www.lcsc.com",
                "env_vars": [],
                "setup_notes": "PENDING: No official API. Internal JSON API can be reverse-engineered. No auth required. Use at own risk.",
            },
            {
                "name": "partfuse", "display_name": "PartFuse (Unified API)",
                "category": "api", "source_type": "aggregator",
                "description": "Unified API on RapidAPI — DigiKey + Mouser + TME in one JSON shape. Simple integration.",
                "signup_url": "https://rapidapi.com/partfuse/api/partfuse",
                "env_vars": [],
                "setup_notes": "PENDING: Sign up on RapidAPI → subscribe to PartFuse. Header-based auth. Good as backup/redundancy for DigiKey+Mouser.",
            },
            {
                "name": "stock_list_import", "display_name": "Vendor Stock List Import",
                "category": "manual", "source_type": "internal",
                "description": "Buyers upload Excel/CSV stock lists from vendors. Auto-parsed and imported as sightings + vendor card enrichment.",
                "signup_url": "",
                "env_vars": [],
                "setup_notes": "PENDING: Build upload UI. Parse common stock list formats (MPN, Qty, Price, Manufacturer). Dedupe against existing data.",
            },

            # ── PENDING (additional authorized distributors) ──
            {
                "name": "newark", "display_name": "Newark / element14 / Farnell",
                "category": "api", "source_type": "authorized",
                "description": "Major authorized distributor (part of Avnet). element14 API covers Newark (Americas), Farnell (Europe), element14 (APAC).",
                "signup_url": "https://partner.element14.com/docs",
                "env_vars": [],
                "setup_notes": "PENDING: Register at element14 Partner API portal. REST API with JSON responses. Single key covers all 3 regional brands.",
            },
            {
                "name": "rs_components", "display_name": "RS Components",
                "category": "api", "source_type": "authorized",
                "description": "Global authorized distributor with Product Search API. 700K+ products across electronics, industrial, and maintenance.",
                "signup_url": "https://developerportal.rs-online.com",
                "env_vars": [],
                "setup_notes": "PENDING: Register at RS Developer Portal. REST API with OAuth2. Covers pricing, stock, and product specs.",
            },
            {
                "name": "future", "display_name": "Future Electronics",
                "category": "api", "source_type": "authorized",
                "description": "Top 3 global authorized distributor. Has developer API program for inventory and pricing.",
                "signup_url": "https://www.futureelectronics.com",
                "env_vars": [],
                "setup_notes": "PENDING: Contact Future Electronics for API partnership. May require established business account.",
            },
            {
                "name": "rochester", "display_name": "Rochester Electronics",
                "category": "api", "source_type": "authorized",
                "description": "Specialist in EOL/obsolete and hard-to-find semiconductors. Licensed manufacturer of discontinued ICs. Critical for legacy parts.",
                "signup_url": "https://www.rocelec.com",
                "env_vars": [],
                "setup_notes": "PENDING: Contact Rochester for API access. Essential for obsolete/EOL part sourcing. Also manufactures discontinued parts under license.",
            },
            {
                "name": "verical", "display_name": "Verical (Arrow Marketplace)",
                "category": "api", "source_type": "marketplace",
                "description": "Arrow Electronics' open marketplace. Connects buyers with verified suppliers beyond Arrow's own stock.",
                "signup_url": "https://www.verical.com",
                "env_vars": [],
                "setup_notes": "PENDING: Check if Verical offers API access through Arrow's developer program. Marketplace model with supplier verification.",
            },
            {
                "name": "heilind", "display_name": "Heilind Electronics",
                "category": "api", "source_type": "authorized",
                "description": "Authorized distributor specializing in connectors, relays, sensors, switches, and thermal management.",
                "signup_url": "https://www.heilind.com",
                "env_vars": [],
                "setup_notes": "PENDING: Contact Heilind for API or EDI integration. Strong in interconnect and electromechanical components.",
            },
            {
                "name": "winsource", "display_name": "WIN SOURCE",
                "category": "api", "source_type": "broker",
                "description": "Global electronic component distributor with 1M+ SKUs. Strong in China-sourced components and hard-to-find parts.",
                "signup_url": "https://www.win-source.net",
                "env_vars": [],
                "setup_notes": "PENDING: Check WIN SOURCE for API access. Large inventory, competitive pricing on Asian-sourced components.",
            },
            {
                "name": "siliconexpert", "display_name": "SiliconExpert / Z2Data",
                "category": "api", "source_type": "intelligence",
                "description": "Component lifecycle intelligence — EOL risk, cross-references, compliance (RoHS/REACH), market availability trends. Not a seller, but critical for material card enrichment.",
                "signup_url": "https://www.siliconexpert.com/api",
                "env_vars": [],
                "setup_notes": "PENDING: API available for component lifecycle data, cross-refs, and compliance. Enriches material cards with lifecycle risk and alternates.",
            },
            {
                "name": "aliexpress", "display_name": "AliExpress",
                "category": "scraper", "source_type": "marketplace",
                "description": "Chinese marketplace with electronic components at reference pricing. Useful for cost benchmarking and identifying Chinese suppliers.",
                "signup_url": "https://developers.aliexpress.com",
                "env_vars": [],
                "setup_notes": "PENDING: AliExpress has affiliate API. Useful for reference pricing and Chinese supplier discovery. Not for mission-critical procurement.",
            },
        ]

        # Version hash — skip if source list hasn't changed
        source_hash = hashlib.md5(str([(s["name"], s["description"]) for s in SOURCES]).encode()).hexdigest()[:12]
        existing_map = {s.name: s for s in db.query(ApiSource).all()}

        # Quick check: if all sources exist and count matches, check version
        if len(existing_map) == len(SOURCES) and all(s["name"] in existing_map for s in SOURCES):
            # All sources present — skip update (descriptions only change on code update)
            log.debug(f"API sources up to date ({len(SOURCES)} sources, hash={source_hash})")
            return

        # Batch fetch all existing sources (1 query instead of 25+)
        log.info(f"Seeding API sources ({len(SOURCES)} sources, hash={source_hash})")
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
        log.warning(f"API source seed error: {e}")
        db.rollback()
    finally:
        db.close()

_seed_api_sources()

# ── Shared dependencies (auth, query helpers) ────────────────────────────
from .dependencies import (
    get_user, require_user, require_buyer, user_reqs_query,
    get_req_for_user, require_fresh_token,
)


@app.get("/api/scheduler-status")
async def scheduler_status(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Diagnostic endpoint — shows scheduler state for all M365 users."""
    if user.email.lower() not in settings.admin_emails:
        raise HTTPException(403, "Admin only")
    users = db.query(User).all()
    result = []
    for u in users:
        result.append({
            "id": u.id, "email": u.email,
            "m365_connected": u.m365_connected,
            "has_refresh_token": bool(u.refresh_token),
            "token_expires_at": u.token_expires_at.isoformat() if u.token_expires_at else None,
            "last_inbox_scan": u.last_inbox_scan.isoformat() if u.last_inbox_scan else None,
            "last_contacts_sync": u.last_contacts_sync.isoformat() if u.last_contacts_sync else None,
        })
    return {"users": result}


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

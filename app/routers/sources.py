"""
routers/sources.py — Data Source Management & Email Mining Intelligence

Manages API source configuration (list, test, toggle) and email mining
endpoints (inbox scan, outbound scan, engagement scoring, attachment parsing).

Business Rules:
- Sources auto-detect status from env vars on list
- Email mining creates/updates VendorCards with discovered contacts
- Attachment parsing creates Sightings via fuzzy MPN matching
- Engagement scores require minimum 2 outreach events (cold-start protection)

Called by: main.py (router mount)
Depends on: models, config, dependencies, connectors/, services/
"""

import base64
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import require_fresh_token, require_settings_access, require_user
from ..rate_limit import limiter
from ..models import (
    ApiSource,
    Requirement,
    Sighting,
    User,
    VendorCard,
    VendorResponse,
)
from ..schemas.responses import SourceListResponse
from ..schemas.sources import MiningOptions, SourceStatusToggle
from ..services.credential_service import get_credential_cached
from ..vendor_utils import normalize_vendor_name

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────


def _get_connector_for_source(name: str, db: Session = None):
    """Instantiate the right connector for a source name.
    Checks DB credentials first, falls back to env vars."""
    from ..connectors.digikey import DigiKeyConnector
    from ..connectors.ebay import EbayConnector
    from ..connectors.mouser import MouserConnector
    from ..connectors.oemsecrets import OEMSecretsConnector
    from ..connectors.sourcengine import SourcengineConnector
    from ..connectors.sources import BrokerBinConnector, NexarConnector
    from ..services.credential_service import get_credential

    def _cred(var_name):
        if db:
            return get_credential(db, name, var_name)
        return os.getenv(var_name) or None

    nexar_id = _cred("NEXAR_CLIENT_ID")
    nexar_sec = _cred("NEXAR_CLIENT_SECRET")
    if name == "nexar" and nexar_id:
        return NexarConnector(nexar_id, nexar_sec)

    bb_key = _cred("BROKERBIN_API_KEY")
    bb_sec = _cred("BROKERBIN_API_SECRET")
    if name == "brokerbin" and bb_key:
        return BrokerBinConnector(bb_key, bb_sec)

    ebay_id = _cred("EBAY_CLIENT_ID")
    ebay_sec = _cred("EBAY_CLIENT_SECRET")
    if name == "ebay" and ebay_id:
        return EbayConnector(ebay_id, ebay_sec)

    dk_id = _cred("DIGIKEY_CLIENT_ID")
    dk_sec = _cred("DIGIKEY_CLIENT_SECRET")
    if name == "digikey" and dk_id:
        return DigiKeyConnector(dk_id, dk_sec)

    mouser_key = _cred("MOUSER_API_KEY")
    if name == "mouser" and mouser_key:
        return MouserConnector(mouser_key)

    oem_key = _cred("OEMSECRETS_API_KEY")
    if name == "oemsecrets" and oem_key:
        return OEMSecretsConnector(oem_key)

    src_key = _cred("SOURCENGINE_API_KEY")
    if name == "sourcengine" and src_key:
        return SourcengineConnector(src_key)

    if name == "email_mining" and settings.email_mining_enabled:
        return _EmailMiningTestConnector()

    if name == "anthropic_ai":
        return _AnthropicTestConnector()
    if name == "acctivate_erp":
        return _AcctivateTestConnector()
    if name == "teams_notifications":
        return _TeamsTestConnector()
    if name == "apollo_enrichment":
        return _ApolloTestConnector()
    if name == "clay_enrichment":
        return _ClayTestConnector()
    if name == "explorium_enrichment":
        return _ExploriumTestConnector()
    if name == "azure_oauth":
        return _AzureOAuthTestConnector()

    return None


class _EmailMiningTestConnector:
    """Thin wrapper so email_mining can be tested via the source test UI."""

    async def search(self, mpn: str) -> list[dict]:
        return [
            {
                "vendor_name": "Email Mining Active",
                "mpn_matched": "Inbox scanned every 30 min",
                "status": "ok",
            }
        ]


class _AnthropicTestConnector:
    """Test Anthropic API key with a lightweight messages call."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        api_key = get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not configured")
        resp = await http.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Reply with only: OK"}],
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise ValueError(f"Anthropic API returned {resp.status_code}: {resp.text[:200]}")
        model = resp.json().get("model", "unknown")
        return [{"vendor_name": "Anthropic AI", "mpn_matched": f"Connected — model: {model}", "status": "ok"}]


class _AcctivateTestConnector:
    """Test Acctivate SQL Server connection with SELECT 1."""

    async def search(self, mpn: str) -> list[dict]:
        import pymssql  # type: ignore

        host = get_credential_cached("acctivate_erp", "ACCTIVATE_HOST")
        port = int(get_credential_cached("acctivate_erp", "ACCTIVATE_PORT") or 1433)
        user = get_credential_cached("acctivate_erp", "ACCTIVATE_USER")
        password = get_credential_cached("acctivate_erp", "ACCTIVATE_PASSWORD")
        database = get_credential_cached("acctivate_erp", "ACCTIVATE_DATABASE")
        if not host or not user:
            raise ValueError("Acctivate credentials not configured")
        conn = pymssql.connect(
            server=host, port=port, user=user, password=password,
            database=database or "", login_timeout=10, timeout=10,
        )
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        finally:
            conn.close()
        return [{"vendor_name": "Acctivate ERP", "mpn_matched": f"Connected to {database}", "status": "ok"}]


class _TeamsTestConnector:
    """Test Teams webhook by posting a test adaptive card."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        webhook_url = get_credential_cached("teams_notifications", "TEAMS_WEBHOOK_URL")
        if not webhook_url:
            raise ValueError("TEAMS_WEBHOOK_URL not configured")
        resp = await http.post(
            webhook_url,
            json={
                "type": "message",
                "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [{"type": "TextBlock", "text": "AVAIL connection test — OK", "wrap": True}],
                    },
                }],
            },
            timeout=15,
        )
        if resp.status_code not in (200, 202):
            raise ValueError(f"Teams webhook returned {resp.status_code}: {resp.text[:200]}")
        return [{"vendor_name": "Teams", "mpn_matched": "Message posted", "status": "ok"}]


class _ApolloTestConnector:
    """Test Apollo API key with a search query."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        api_key = get_credential_cached("apollo_enrichment", "APOLLO_API_KEY")
        if not api_key:
            raise ValueError("APOLLO_API_KEY not configured")
        resp = await http.post(
            "https://api.apollo.io/v1/mixed_people/search",
            headers={"Content-Type": "application/json"},
            json={"api_key": api_key, "q_organization_domains": ["anthropic.com"], "page": 1, "per_page": 1},
            timeout=15,
        )
        if resp.status_code != 200:
            raise ValueError(f"Apollo API returned {resp.status_code}: {resp.text[:200]}")
        count = len(resp.json().get("people", []))
        return [{"vendor_name": "Apollo", "mpn_matched": f"Search OK — {count} result(s)", "status": "ok"}]


class _ClayTestConnector:
    """Test Clay API key with a company enrichment call."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        api_key = get_credential_cached("clay_enrichment", "CLAY_API_KEY")
        if not api_key:
            raise ValueError("CLAY_API_KEY not configured")
        resp = await http.post(
            "https://api.clay.com/v3/sources/enrich-company",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"domain": "anthropic.com"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise ValueError(f"Clay API returned {resp.status_code}: {resp.text[:200]}")
        name = resp.json().get("name", "Unknown")
        return [{"vendor_name": "Clay", "mpn_matched": f"Enriched: {name}", "status": "ok"}]


class _ExploriumTestConnector:
    """Test Explorium API key with a business match call."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        api_key = get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        if not api_key:
            raise ValueError("EXPLORIUM_API_KEY not configured")
        resp = await http.post(
            "https://api.explorium.ai/v1/match/business",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"domain": "anthropic.com"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise ValueError(f"Explorium API returned {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        name = data.get("firmo_name", data.get("name", "matched"))
        return [{"vendor_name": "Explorium", "mpn_matched": f"Match: {name}", "status": "ok"}]


class _AzureOAuthTestConnector:
    """Test Azure tenant by fetching OpenID configuration."""

    async def search(self, mpn: str) -> list[dict]:
        from ..http_client import http

        tenant_id = settings.azure_tenant_id
        if not tenant_id:
            raise ValueError("AZURE_TENANT_ID not configured")
        url = f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
        resp = await http.get(url, timeout=10)
        if resp.status_code != 200:
            raise ValueError(f"Azure OpenID discovery returned {resp.status_code}")
        issuer = resp.json().get("issuer", "")
        if tenant_id not in issuer:
            raise ValueError(f"Tenant mismatch in issuer: {issuer}")
        return [{"vendor_name": "Azure OAuth", "mpn_matched": "Tenant verified", "status": "ok"}]


def _create_sightings_from_attachment(
    db: Session,
    vr: VendorResponse,
    rows: list[dict],
) -> int:
    """Create Sighting records from parsed attachment rows, matching to Requirements."""
    from ..utils.normalization import (
        detect_currency,
        fuzzy_mpn_match,
        normalize_condition,
        normalize_date_code,
        normalize_lead_time,
        normalize_mpn,
        normalize_packaging,
        normalize_price,
        normalize_quantity,
    )

    reqs = db.query(Requirement).filter_by(requisition_id=vr.requisition_id).all()
    if not reqs:
        return 0

    req_map: dict[str, Requirement] = {}
    for req in reqs:
        norm = (req.mpn or "").upper().strip()
        if norm:
            req_map[norm] = req

    created = 0
    for row in rows:
        mpn = (row.get("mpn") or "").upper().strip()
        if not mpn:
            continue

        matched_req = req_map.get(mpn)
        if not matched_req:
            for req_mpn, req in req_map.items():
                if fuzzy_mpn_match(mpn, req_mpn):
                    matched_req = req
                    break

        if not matched_req:
            continue

        existing = (
            db.query(Sighting)
            .filter_by(
                requirement_id=matched_req.id,
                vendor_name=vr.vendor_name or "",
                mpn_matched=mpn,
                source_type="email_attachment",
            )
            .first()
        )
        if existing:
            continue

        sighting = Sighting(
            requirement_id=matched_req.id,
            vendor_name=vr.vendor_name or "",
            vendor_email=vr.vendor_email,
            mpn_matched=normalize_mpn(mpn) or mpn,
            manufacturer=row.get("manufacturer", ""),
            qty_available=normalize_quantity(row.get("qty")),
            unit_price=normalize_price(row.get("unit_price")),
            currency=detect_currency(row.get("currency") or row.get("unit_price")),
            moq=normalize_quantity(row.get("moq")),
            source_type="email_attachment",
            condition=normalize_condition(row.get("condition")),
            date_code=normalize_date_code(row.get("date_code")),
            packaging=normalize_packaging(row.get("packaging")),
            lead_time_days=normalize_lead_time(row.get("lead_time")),
            lead_time=row.get("lead_time"),
            confidence=0.7,
            raw_data=row,
        )
        db.add(sighting)
        created += 1

    db.flush()
    return created


# ══════════════════════════════════════════════════════════════════════
# API SOURCES — Data Source Management & Tracking
# ══════════════════════════════════════════════════════════════════════


@router.get("/api/sources", response_model=SourceListResponse, response_model_exclude_none=True)
async def list_api_sources(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Return all API sources grouped by status."""
    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()

    from ..services.credential_service import credential_is_set

    for src in sources:
        env_vars = src.env_vars or []
        if env_vars:
            all_set = all(credential_is_set(db, src.name, v) for v in env_vars)
            any_set = any(credential_is_set(db, src.name, v) for v in env_vars)
            if all_set and src.status == "pending":
                src.status = "live"
            elif not any_set and src.status == "live":
                src.status = "pending"
    db.commit()

    result = []
    for src in sources:
        env_status = {}
        for v in src.env_vars or []:
            env_status[v] = credential_is_set(db, src.name, v)

        result.append(
            {
                "id": src.id,
                "name": src.name,
                "display_name": src.display_name,
                "category": src.category,
                "source_type": src.source_type,
                "status": src.status,
                "description": src.description,
                "setup_notes": src.setup_notes,
                "signup_url": src.signup_url,
                "env_vars": src.env_vars or [],
                "env_status": env_status,
                "last_success": src.last_success.isoformat()
                if src.last_success
                else None,
                "last_error": src.last_error,
                "total_searches": src.total_searches or 0,
                "total_results": src.total_results or 0,
                "avg_response_ms": src.avg_response_ms or 0,
                "created_at": src.created_at.isoformat() if src.created_at else None,
            }
        )

    return {"sources": result}


@router.post("/api/sources/{source_id}/test")
@limiter.limit("5/minute")
async def test_api_source(
    source_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Test a specific API source with a known part number."""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "API source not found")

    test_mpn = "LM358N"
    start = time.time()
    results = []
    error = None

    try:
        connector = _get_connector_for_source(src.name, db)
        if not connector:
            raise ValueError(f"No connector available for {src.name}")
        results = await connector.search(test_mpn)
        elapsed_ms = int((time.time() - start) * 1000)

        src.status = "live"
        src.last_success = datetime.now(timezone.utc)
        src.last_error = None
        src.avg_response_ms = elapsed_ms
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error = str(e)[:500]
        src.status = "error"
        src.last_error = error

    db.commit()

    return {
        "source": src.display_name,
        "test_mpn": test_mpn,
        "status": "ok" if results else "no_results" if not error else "error",
        "results_count": len(results),
        "elapsed_ms": elapsed_ms,
        "error": error,
        "sample": results[:3] if results else [],
    }


@router.put("/api/sources/{source_id}/toggle")
async def toggle_api_source(
    source_id: int,
    payload: SourceStatusToggle,
    user: User = Depends(require_settings_access),
    db: Session = Depends(get_db),
):
    """Enable or disable a source (admin + dev_assistant)."""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404, "API source not found")
    src.status = payload.status
    db.commit()
    return {"ok": True, "status": src.status}


# ══════════════════════════════════════════════════════════════════════
# EMAIL INTELLIGENCE — Inbox Mining
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/email-mining/scan")
@limiter.limit("2/minute")
async def scan_inbox_for_vendors(
    request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Run email intelligence scan — mines inbox for vendor contacts and offers."""
    token = await require_fresh_token(request, db)

    opts = MiningOptions()
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.body()
            if body and body.strip():
                opts = MiningOptions.model_validate_json(body)
    except (ValueError, TypeError):
        logger.debug("Mining options parse failed, using defaults", exc_info=True)
    lookback_days = opts.lookback_days

    from ..connectors.email_mining import EmailMiner
    from ..vendor_utils import merge_emails_into_card, merge_phones_into_card

    miner = EmailMiner(token)
    results = await miner.scan_inbox(lookback_days=lookback_days, max_messages=500)

    enriched_count = 0
    for contact in results.get("contacts_enriched", []):
        vendor_name = contact.get("vendor_name", "")
        if not vendor_name:
            continue

        norm = normalize_vendor_name(vendor_name)
        card = db.query(VendorCard).filter_by(normalized_name=norm).first()
        if not card:
            card = VendorCard(
                normalized_name=norm,
                display_name=vendor_name,
                emails=[],
                phones=[],
                source="email_mining",
            )
            db.add(card)
            db.flush()

        enriched_count += merge_emails_into_card(card, contact.get("emails", []))
        merge_phones_into_card(card, contact.get("phones", []))

        websites = contact.get("websites", [])
        if not card.website and websites:
            card.website = f"https://{websites[0]}"

    db.commit()

    em_src = db.query(ApiSource).filter_by(name="email_mining").first()
    if em_src:
        em_src.last_success = datetime.now(timezone.utc)
        em_src.total_searches = (em_src.total_searches or 0) + 1
        em_src.total_results = (em_src.total_results or 0) + results.get(
            "vendors_found", 0
        )
        em_src.status = "live"
        db.commit()

    return {
        "messages_scanned": results.get("messages_scanned", 0),
        "vendors_found": results.get("vendors_found", 0),
        "offers_parsed": len(results.get("offers_parsed", [])),
        "contacts_enriched": enriched_count,
        "stock_lists_found": results.get("stock_lists_found", 0),
    }


@router.get("/api/email-mining/status")
async def email_mining_status(
    user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """Get current email mining status."""
    src = db.query(ApiSource).filter_by(name="email_mining").first()
    return {
        "enabled": settings.email_mining_enabled,
        "last_scan": src.last_success.isoformat() if src and src.last_success else None,
        "total_scans": src.total_searches if src else 0,
        "total_vendors_found": src.total_results if src else 0,
    }


@router.post("/api/email-mining/scan-outbound")
@limiter.limit("2/minute")
async def email_mining_scan_outbound(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Scan Sent Items for AVAIL RFQ emails, update VendorCard outreach metrics."""
    if not user.m365_connected or not user.access_token:
        raise HTTPException(400, "M365 not connected")

    from ..connectors.email_mining import EmailMiner
    from ..scheduler import get_valid_token

    token = await get_valid_token(user, db) or user.access_token
    opts = MiningOptions()
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            raw = await request.body()
            if raw and raw.strip():
                opts = MiningOptions.model_validate_json(raw)
    except (ValueError, TypeError):
        logger.debug("Mining options parse failed, using defaults", exc_info=True)
    lookback = opts.lookback_days

    miner = EmailMiner(token, db=db, user_id=user.id)
    results = await miner.scan_sent_items(lookback_days=lookback, max_messages=500)

    vendors_contacted = results.get("vendors_contacted", {})
    cards_updated = 0
    for domain, count in vendors_contacted.items():
        card = db.query(VendorCard).filter(VendorCard.domain == domain).first()
        if not card:
            prefix = domain.split(".")[0].lower() if "." in domain else domain
            card = (
                db.query(VendorCard)
                .filter(VendorCard.normalized_name == prefix)
                .first()
            )
        if card:
            card.total_outreach = (card.total_outreach or 0) + count
            card.last_contact_at = datetime.now(timezone.utc)
            cards_updated += 1

    try:
        db.commit()
    except SQLAlchemyError:
        logger.exception("DB commit failed during email scan")
        db.rollback()

    return {
        "messages_scanned": results.get("messages_scanned", 0),
        "rfqs_detected": results.get("rfqs_detected", 0),
        "vendors_contacted": len(vendors_contacted),
        "cards_updated": cards_updated,
        "used_delta": results.get("used_delta", False),
    }


@router.post("/api/email-mining/compute-engagement")
@limiter.limit("2/minute")
async def email_mining_compute_engagement(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Manually trigger engagement score recomputation for all vendor cards."""
    from ..services.engagement_scorer import compute_all_engagement_scores

    result = await compute_all_engagement_scores(db)
    return {
        "updated": result.get("updated", 0),
        "skipped": result.get("skipped", 0),
    }


@router.get("/api/vendors/{vendor_id}/engagement")
async def vendor_engagement_detail(
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get detailed engagement breakdown for a specific vendor."""
    from ..services.engagement_scorer import compute_engagement_score

    card = db.query(VendorCard).filter_by(id=vendor_id).first()
    if not card:
        raise HTTPException(404, "Vendor not found")

    result = compute_engagement_score(
        total_outreach=card.total_outreach or 0,
        total_responses=card.total_responses or 0,
        total_wins=card.total_wins or 0,
        avg_velocity_hours=card.response_velocity_hours,
        last_contact_at=card.last_contact_at,
    )

    return {
        "vendor_id": card.id,
        "vendor_name": card.display_name,
        "engagement_score": result["engagement_score"],
        "metrics": {
            "response_rate": result["response_rate"],
            "ghost_rate": result["ghost_rate"],
            "recency_score": result["recency_score"],
            "velocity_score": result["velocity_score"],
            "win_rate": result["win_rate"],
        },
        "raw_counts": {
            "total_outreach": card.total_outreach or 0,
            "total_responses": card.total_responses or 0,
            "total_wins": card.total_wins or 0,
            "response_velocity_hours": card.response_velocity_hours,
            "relationship_months": card.relationship_months,
            "last_contact_at": card.last_contact_at.isoformat()
            if card.last_contact_at
            else None,
        },
        "computed_at": card.engagement_computed_at.isoformat()
        if card.engagement_computed_at
        else None,
    }


@router.post("/api/email-mining/parse-response-attachments/{response_id}")
async def parse_response_attachments(
    response_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Download and parse attachments from a vendor response email.
    Creates Sightings for matched MPNs via AI-powered column mapping."""
    if not user.m365_connected or not user.access_token:
        raise HTTPException(400, "M365 not connected")

    vr = db.query(VendorResponse).filter_by(id=response_id).first()
    if not vr:
        raise HTTPException(404, "Response not found")

    if not vr.message_id:
        raise HTTPException(400, "No message ID — cannot fetch attachments")

    from ..scheduler import get_valid_token
    from ..utils.graph_client import GraphClient

    token = await get_valid_token(user, db) or user.access_token
    gc = GraphClient(token)

    try:
        att_data = await gc.get_json(f"/me/messages/{vr.message_id}/attachments")
    except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:
        raise HTTPException(502, f"Graph API error: {str(e)[:200]}")

    attachments = att_data.get("value", []) if att_data else []
    parseable_exts = {".xlsx", ".xls", ".csv", ".tsv"}
    parseable = [
        a
        for a in attachments
        if any((a.get("name") or "").lower().endswith(ext) for ext in parseable_exts)
    ]

    if not parseable:
        return {
            "attachments_found": len(attachments),
            "parseable": 0,
            "rows_parsed": 0,
            "sightings_created": 0,
        }

    from ..services.attachment_parser import parse_attachment
    from ..utils.file_validation import validate_file

    vendor_domain = ""
    if vr.vendor_email and "@" in vr.vendor_email:
        vendor_domain = vr.vendor_email.split("@", 1)[1].lower()

    total_rows = 0
    sightings_created = 0

    for att in parseable:
        content_bytes = att.get("contentBytes")
        if not content_bytes:
            continue

        file_bytes = base64.b64decode(content_bytes)
        filename = att.get("name", "unknown.xlsx")

        is_valid, _ = validate_file(file_bytes, filename)
        if not is_valid:
            continue

        rows = await parse_attachment(
            file_bytes, filename, vendor_domain=vendor_domain, db=db
        )
        total_rows += len(rows)

        if vr.requisition_id and rows:
            sightings_created += _create_sightings_from_attachment(db, vr, rows)

    try:
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        raise HTTPException(500, f"Save failed: {str(e)[:200]}")

    return {
        "attachments_found": len(attachments),
        "parseable": len(parseable),
        "rows_parsed": total_rows,
        "sightings_created": sightings_created,
    }

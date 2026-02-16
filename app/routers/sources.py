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
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..dependencies import require_fresh_token, require_user
from ..schemas.sources import MiningOptions, SourceStatusToggle
from ..models import (
    ApiSource,
    Requirement,
    Sighting,
    User,
    VendorCard,
    VendorResponse,
)
from ..vendor_utils import normalize_vendor_name

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────


def _get_connector_for_source(name: str, db: Session = None):
    """Instantiate the right connector for a source name.
    Checks DB credentials first, falls back to env vars."""
    from ..connectors.sources import NexarConnector, BrokerBinConnector
    from ..connectors.ebay import EbayConnector
    from ..connectors.digikey import DigiKeyConnector
    from ..connectors.mouser import MouserConnector
    from ..connectors.oemsecrets import OEMSecretsConnector
    from ..connectors.sourcengine import SourcengineConnector
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
    return None


class _EmailMiningTestConnector:
    """Thin wrapper so email_mining can be tested via the source test UI."""

    async def search(self, mpn: str) -> list[dict]:
        return [{"vendor_name": "Email Mining Active", "mpn_matched": "Inbox scanned every 30 min", "status": "ok"}]


def _create_sightings_from_attachment(
    db: Session,
    vr: VendorResponse,
    rows: list[dict],
) -> int:
    """Create Sighting records from parsed attachment rows, matching to Requirements."""
    from ..utils.normalization import fuzzy_mpn_match

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

        existing = db.query(Sighting).filter_by(
            requirement_id=matched_req.id,
            vendor_name=vr.vendor_name or "",
            mpn_matched=mpn,
            source_type="email_attachment",
        ).first()
        if existing:
            continue

        sighting = Sighting(
            requirement_id=matched_req.id,
            vendor_name=vr.vendor_name or "",
            vendor_email=vr.vendor_email,
            mpn_matched=mpn,
            manufacturer=row.get("manufacturer", ""),
            qty_available=row.get("qty"),
            unit_price=row.get("unit_price"),
            currency=row.get("currency", "USD"),
            moq=row.get("moq"),
            source_type="email_attachment",
            condition=row.get("condition"),
            date_code=row.get("date_code"),
            packaging=row.get("packaging"),
            lead_time_days=row.get("lead_time_days"),
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


@router.get("/api/sources")
async def list_api_sources(user: User = Depends(require_user), db: Session = Depends(get_db)):
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
        for v in (src.env_vars or []):
            env_status[v] = credential_is_set(db, src.name, v)

        result.append({
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
            "last_success": src.last_success.isoformat() if src.last_success else None,
            "last_error": src.last_error,
            "total_searches": src.total_searches or 0,
            "total_results": src.total_results or 0,
            "avg_response_ms": src.avg_response_ms or 0,
            "created_at": src.created_at.isoformat() if src.created_at else None,
        })

    return {"sources": result}


@router.post("/api/sources/{source_id}/test")
async def test_api_source(source_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Test a specific API source with a known part number."""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404)

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
async def toggle_api_source(source_id: int, payload: SourceStatusToggle, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Enable or disable a source."""
    src = db.get(ApiSource, source_id)
    if not src:
        raise HTTPException(404)
    src.status = payload.status
    db.commit()
    return {"ok": True, "status": src.status}


# ══════════════════════════════════════════════════════════════════════
# EMAIL INTELLIGENCE — Inbox Mining
# ══════════════════════════════════════════════════════════════════════


@router.post("/api/email-mining/scan")
async def scan_inbox_for_vendors(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Run email intelligence scan — mines inbox for vendor contacts and offers."""
    token = await require_fresh_token(request, db)

    opts = MiningOptions()
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            body = await request.body()
            if body and body.strip():
                opts = MiningOptions.model_validate_json(body)
    except Exception:
        pass
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
                emails=[], phones=[],
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
        em_src.total_results = (em_src.total_results or 0) + results.get("vendors_found", 0)
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
async def email_mining_status(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Get current email mining status."""
    src = db.query(ApiSource).filter_by(name="email_mining").first()
    return {
        "enabled": settings.email_mining_enabled,
        "last_scan": src.last_success.isoformat() if src and src.last_success else None,
        "total_scans": src.total_searches if src else 0,
        "total_vendors_found": src.total_results if src else 0,
    }


@router.post("/api/email-mining/scan-outbound")
async def email_mining_scan_outbound(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Scan Sent Items for AVAIL RFQ emails, update VendorCard outreach metrics."""
    if not user.m365_connected or not user.access_token:
        return JSONResponse(status_code=400, content={"error": "M365 not connected"})

    from ..connectors.email_mining import EmailMiner
    from ..scheduler import get_valid_token

    token = await get_valid_token(user, db) or user.access_token
    opts = MiningOptions()
    try:
        if request.headers.get("content-type", "").startswith("application/json"):
            raw = await request.body()
            if raw and raw.strip():
                opts = MiningOptions.model_validate_json(raw)
    except Exception:
        pass
    lookback = opts.lookback_days

    miner = EmailMiner(token, db=db, user_id=user.id)
    results = await miner.scan_sent_items(lookback_days=lookback, max_messages=500)

    vendors_contacted = results.get("vendors_contacted", {})
    cards_updated = 0
    for domain, count in vendors_contacted.items():
        card = db.query(VendorCard).filter(VendorCard.domain == domain).first()
        if not card:
            prefix = domain.split(".")[0].lower() if "." in domain else domain
            card = db.query(VendorCard).filter(VendorCard.normalized_name == prefix).first()
        if card:
            card.total_outreach = (card.total_outreach or 0) + count
            card.last_contact_at = datetime.now(timezone.utc)
            cards_updated += 1

    try:
        db.commit()
    except Exception:
        db.rollback()

    return {
        "messages_scanned": results.get("messages_scanned", 0),
        "rfqs_detected": results.get("rfqs_detected", 0),
        "vendors_contacted": len(vendors_contacted),
        "cards_updated": cards_updated,
        "used_delta": results.get("used_delta", False),
    }


@router.post("/api/email-mining/compute-engagement")
async def email_mining_compute_engagement(
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
        return JSONResponse(status_code=404, content={"error": "Vendor not found"})

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
            "last_contact_at": card.last_contact_at.isoformat() if card.last_contact_at else None,
        },
        "computed_at": card.engagement_computed_at.isoformat() if card.engagement_computed_at else None,
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
        return JSONResponse(status_code=400, content={"error": "M365 not connected"})

    vr = db.query(VendorResponse).filter_by(id=response_id).first()
    if not vr:
        return JSONResponse(status_code=404, content={"error": "Response not found"})

    if not vr.message_id:
        return JSONResponse(status_code=400, content={"error": "No message ID — cannot fetch attachments"})

    from ..utils.graph_client import GraphClient
    from ..scheduler import get_valid_token

    token = await get_valid_token(user, db) or user.access_token
    gc = GraphClient(token)

    try:
        att_data = await gc.get_json(f"/me/messages/{vr.message_id}/attachments")
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": f"Graph API error: {str(e)[:200]}"})

    attachments = att_data.get("value", []) if att_data else []
    parseable_exts = {".xlsx", ".xls", ".csv", ".tsv"}
    parseable = [a for a in attachments if any(
        (a.get("name") or "").lower().endswith(ext) for ext in parseable_exts
    )]

    if not parseable:
        return {"attachments_found": len(attachments), "parseable": 0, "rows_parsed": 0, "sightings_created": 0}

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

        rows = await parse_attachment(file_bytes, filename, vendor_domain=vendor_domain, db=db)
        total_rows += len(rows)

        if vr.requisition_id and rows:
            sightings_created += _create_sightings_from_attachment(db, vr, rows)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"error": f"Save failed: {str(e)[:200]}"})

    return {
        "attachments_found": len(attachments),
        "parseable": len(parseable),
        "rows_parsed": total_rows,
        "sightings_created": sightings_created,
    }

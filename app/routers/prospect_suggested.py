"""Prospect suggested accounts router — browse, claim, dismiss from prospect_accounts.

Phase 6: replaces the old Company-based pool with the unified prospect_accounts table.
Phase 7: enhanced claim with deep enrichment, AI briefing, manual submission.
"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import Company, User
from ..models.discovery_batch import DiscoveryBatch
from ..models.prospect_account import ProspectAccount
from ..services.prospect_claim import (
    add_prospect_manually,
    check_enrichment_status,
    claim_prospect,
    trigger_deep_enrichment_bg,
)

router = APIRouter()


@router.get("/api/prospects/suggested")
async def list_suggested(
    search: str = "",
    region: str = "",
    industry: str = "",
    min_fit_score: int = 0,
    readiness_level: str = "",
    status: str = "suggested",
    sort: str = "readiness_desc",
    page: int = 1,
    per_page: int = 20,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List prospect accounts from the unified pool.

    Filters: search, region, industry, min_fit_score, readiness_level, status.
    Sort: readiness_desc (default), fit_desc, composite_desc, name_asc.
    """
    page = max(1, page)
    per_page = min(max(1, per_page), 100)

    query = db.query(ProspectAccount).filter(
        ProspectAccount.status == status,
    )

    if search:
        safe = search.strip().replace("%", r"\%").replace("_", r"\_")
        query = query.filter(
            ProspectAccount.name.ilike(f"%{safe}%")
            | ProspectAccount.domain.ilike(f"%{safe}%")
            | ProspectAccount.industry.ilike(f"%{safe}%")
        )

    if region:
        query = query.filter(ProspectAccount.region == region)

    if industry:
        safe = industry.strip().replace("%", r"\%").replace("_", r"\_")
        query = query.filter(ProspectAccount.industry.ilike(f"%{safe}%"))

    if min_fit_score > 0:
        query = query.filter(ProspectAccount.fit_score >= min_fit_score)

    if readiness_level:
        if readiness_level == "call_now":
            query = query.filter(ProspectAccount.readiness_score >= 70)
        elif readiness_level == "nurture":
            query = query.filter(
                ProspectAccount.readiness_score >= 40,
                ProspectAccount.readiness_score < 70,
            )
        elif readiness_level == "monitor":
            query = query.filter(ProspectAccount.readiness_score < 40)

    total = query.count()

    # Sort
    if sort == "fit_desc":
        query = query.order_by(ProspectAccount.fit_score.desc(), ProspectAccount.readiness_score.desc())
    elif sort == "name_asc":
        query = query.order_by(ProspectAccount.name)
    elif sort == "composite_desc":
        # 60% fit + 40% readiness
        query = query.order_by(
            (ProspectAccount.fit_score * 0.6 + ProspectAccount.readiness_score * 0.4).desc()
        )
    else:  # readiness_desc (default)
        query = query.order_by(ProspectAccount.readiness_score.desc(), ProspectAccount.fit_score.desc())

    offset = (page - 1) * per_page
    prospects = query.offset(offset).limit(per_page).all()

    items = [_serialize_prospect(p) for p in prospects]

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/api/prospects/suggested/stats")
async def suggested_stats(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Aggregate stats for the Suggested tab header."""
    base = db.query(ProspectAccount).filter(ProspectAccount.status == "suggested")
    total = base.count()
    call_now = base.filter(ProspectAccount.readiness_score >= 70).count()
    nurture = base.filter(
        ProspectAccount.readiness_score >= 40,
        ProspectAccount.readiness_score < 70,
    ).count()
    high_fit = base.filter(ProspectAccount.fit_score >= 70).count()

    # Claimed this month
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    claimed = (
        db.query(func.count(ProspectAccount.id))
        .filter(
            ProspectAccount.status == "claimed",
            ProspectAccount.claimed_at >= month_start,
        )
        .scalar()
        or 0
    )

    return {
        "total_available": total,
        "call_now_count": call_now,
        "nurture_count": nurture,
        "high_fit_count": high_fit,
        "claimed_this_month": claimed,
    }


@router.get("/api/prospects/suggested/{prospect_id}")
async def get_suggested_detail(
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Full prospect detail including all enrichment data."""
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    result = _serialize_prospect(prospect)
    # Include full enrichment for detail view
    result["fit_reasoning"] = prospect.fit_reasoning
    result["enrichment_data"] = prospect.enrichment_data or {}
    result["historical_context"] = prospect.historical_context or {}
    return result


@router.post("/api/prospects/suggested/{prospect_id}/claim")
async def claim_suggested(
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Claim a prospect account with deep enrichment.

    SF-migrated (company_id set): update Company.account_owner_id.
    New discovery (company_id NULL): create Company record, link it.
    Domain collision: if Company with same domain exists, link to it.

    Triggers background deep enrichment (contact reveal + AI briefing).
    """
    try:
        result = claim_prospect(prospect_id, user.id, db)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))

    # Trigger deep enrichment in background
    asyncio.create_task(trigger_deep_enrichment_bg(prospect_id))

    return result


@router.post("/api/prospects/suggested/{prospect_id}/dismiss")
async def dismiss_suggested(
    prospect_id: int,
    payload: dict,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Dismiss a prospect with a reason."""
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    if prospect.status != "suggested":
        raise HTTPException(409, "Can only dismiss suggested prospects")

    reason = (payload.get("reason") or "other").strip()

    prospect.status = "dismissed"
    prospect.dismissed_by = user.id
    prospect.dismissed_at = datetime.now(timezone.utc)
    prospect.dismiss_reason = reason
    db.commit()

    logger.info("User {} dismissed prospect {} reason={}", user.name, prospect.name, reason)

    return {
        "prospect_id": prospect.id,
        "company_name": prospect.name,
        "status": "dismissed",
    }


# ── Phase 7 Endpoints ───────────────────────────────────────────────


@router.get("/api/prospects/suggested/{prospect_id}/enrichment")
async def get_enrichment_status(
    prospect_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Poll enrichment status after a claim."""
    try:
        return check_enrichment_status(prospect_id, db)
    except LookupError:
        raise HTTPException(404, "Prospect not found")


@router.post("/api/prospects/add")
async def add_prospect(
    payload: dict,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Submit a domain manually for prospecting.

    Body: {"domain": "company.com"}
    Deduplicates against existing prospect_accounts.
    """
    domain = (payload.get("domain") or "").strip()
    if not domain:
        raise HTTPException(400, "Domain is required")

    try:
        return add_prospect_manually(domain, user.id, db)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/prospects/batches")
async def list_batches(
    page: int = 1,
    per_page: int = 20,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List discovery batch history (admin view)."""
    page = max(1, page)
    per_page = min(max(1, per_page), 50)

    total = db.query(func.count(DiscoveryBatch.id)).scalar() or 0
    batches = (
        db.query(DiscoveryBatch)
        .order_by(DiscoveryBatch.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )

    return {
        "items": [
            {
                "id": b.id,
                "batch_id": b.batch_id,
                "source": b.source,
                "segment": b.segment,
                "regions": b.regions or [],
                "status": b.status,
                "prospects_found": b.prospects_found,
                "prospects_new": b.prospects_new,
                "credits_used": b.credits_used,
                "started_at": b.started_at.isoformat() if b.started_at else None,
                "completed_at": b.completed_at.isoformat() if b.completed_at else None,
            }
            for b in batches
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def _serialize_prospect(p: ProspectAccount) -> dict:
    """Serialize a prospect for the card grid."""
    signals = p.readiness_signals or {}
    contacts = p.contacts_preview or []
    similar = p.similar_customers or []

    # Readiness tier
    if p.readiness_score >= 70:
        readiness_tier = "call_now"
    elif p.readiness_score >= 40:
        readiness_tier = "nurture"
    else:
        readiness_tier = "monitor"

    # Signal summary for card display
    signal_tags = []
    intent = signals.get("intent", {})
    if isinstance(intent, dict) and intent.get("strength") in ("strong", "moderate"):
        signal_tags.append({"type": "intent", "label": f"Intent: {intent['strength']}"})
    hiring = signals.get("hiring", {})
    if isinstance(hiring, dict) and hiring.get("type"):
        signal_tags.append({"type": "hiring", "label": f"Hiring: {hiring['type']}"})
    events = signals.get("events", [])
    if isinstance(events, list) and events:
        event_types = [e.get("type", "") for e in events[:2] if isinstance(e, dict)]
        if event_types:
            signal_tags.append({"type": "event", "label": ", ".join(event_types)})

    # Contacts summary
    verified_count = sum(1 for c in contacts if isinstance(c, dict) and c.get("verified"))
    dm_count = sum(1 for c in contacts if isinstance(c, dict) and c.get("seniority") == "decision_maker")

    return {
        "id": p.id,
        "name": p.name,
        "domain": p.domain,
        "website": p.website,
        "industry": p.industry,
        "employee_count_range": p.employee_count_range,
        "revenue_range": p.revenue_range,
        "hq_location": p.hq_location,
        "region": p.region,
        "fit_score": p.fit_score or 0,
        "readiness_score": p.readiness_score or 0,
        "readiness_tier": readiness_tier,
        "signal_tags": signal_tags,
        "contacts_count": len(contacts),
        "contacts_verified": verified_count,
        "contacts_decision_makers": dm_count,
        "contacts_preview": contacts[:3],
        "similar_customers": similar[:3],
        "ai_writeup": p.ai_writeup,
        "discovery_source": p.discovery_source,
        "import_priority": p.import_priority,
        "company_id": p.company_id,
    }

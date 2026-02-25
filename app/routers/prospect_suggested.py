"""Prospect suggested accounts router — browse, claim, dismiss from prospect_accounts.

Phase 6: replaces the old Company-based pool with the unified prospect_accounts table.
Serves enriched prospect cards with fit/readiness scores, signals, contacts preview,
AI writeups, and similar customers.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import Company, User
from ..models.prospect_account import ProspectAccount

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
    """Claim a prospect account.

    SF-migrated (company_id set): update Company.account_owner_id.
    New discovery (company_id NULL): create Company record, link it.
    """
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")

    if prospect.status == "claimed":
        raise HTTPException(409, "Already claimed")

    if prospect.company_id:
        # SF-migrated: update existing Company
        company = db.get(Company, prospect.company_id)
        if company:
            company.account_owner_id = user.id
    else:
        # New discovery: create Company record
        company = Company(
            name=prospect.name,
            domain=prospect.domain,
            website=prospect.website,
            industry=prospect.industry,
            hq_city=prospect.hq_location.split(",")[0].strip() if prospect.hq_location and "," in prospect.hq_location else None,
            is_active=True,
            account_owner_id=user.id,
            source="prospecting",
        )
        db.add(company)
        db.flush()
        prospect.company_id = company.id

    prospect.status = "claimed"
    prospect.claimed_by = user.id
    prospect.claimed_at = datetime.now(timezone.utc)
    db.commit()

    logger.info("User {} claimed prospect {} ({})", user.name, prospect.name, prospect.id)

    return {
        "prospect_id": prospect.id,
        "company_id": prospect.company_id,
        "company_name": prospect.name,
        "status": "claimed",
    }


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

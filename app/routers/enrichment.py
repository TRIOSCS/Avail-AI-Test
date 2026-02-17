"""Enrichment API — review queue, backfill jobs, on-demand enrichment, stats."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_admin, require_user
from ..models import (
    ActivityLog,
    EnrichmentJob,
    EnrichmentQueue,
    Sighting,
    User,
    VendorCard,
    VendorContact,
    Company,
)
from ..schemas.enrichment import (
    BackfillRequest,
    BulkApproveRequest,
)

router = APIRouter(tags=["enrichment"])
log = logging.getLogger(__name__)


# ── Queue endpoints ──────────────────────────────────────────────────


@router.get("/api/enrichment/queue")
def api_list_queue(
    status: str = Query("pending", pattern="^(pending|approved|rejected|auto_applied|all)$"),
    entity_type: str = Query(None),
    source: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List enrichment queue items with filters."""
    q = db.query(EnrichmentQueue)

    if status != "all":
        q = q.filter(EnrichmentQueue.status == status)
    if entity_type == "vendor":
        q = q.filter(EnrichmentQueue.vendor_card_id.isnot(None))
    elif entity_type == "company":
        q = q.filter(EnrichmentQueue.company_id.isnot(None))
    if source:
        q = q.filter(EnrichmentQueue.source == source)

    total = q.count()
    items = q.order_by(EnrichmentQueue.created_at.desc()).offset(offset).limit(limit).all()

    results = []
    for item in items:
        entity_type_str = None
        entity_name = None
        if item.vendor_card_id:
            entity_type_str = "vendor"
            card = db.get(VendorCard, item.vendor_card_id)
            entity_name = card.display_name if card else f"Vendor #{item.vendor_card_id}"
        elif item.company_id:
            entity_type_str = "company"
            company = db.get(Company, item.company_id)
            entity_name = company.name if company else f"Company #{item.company_id}"
        elif item.vendor_contact_id:
            entity_type_str = "contact"

        results.append({
            "id": item.id,
            "entity_type": entity_type_str,
            "entity_name": entity_name,
            "enrichment_type": item.enrichment_type,
            "field_name": item.field_name,
            "current_value": item.current_value,
            "proposed_value": item.proposed_value,
            "confidence": item.confidence,
            "source": item.source,
            "status": item.status,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        })

    return {"items": results, "total": total, "limit": limit, "offset": offset}


@router.post("/api/enrichment/queue/{item_id}/approve")
def api_approve_item(
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Approve and apply an enrichment queue item."""
    from ..services.deep_enrichment_service import apply_queue_item

    item = db.get(EnrichmentQueue, item_id)
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status not in ("pending", "low_confidence"):
        raise HTTPException(400, f"Cannot approve item with status '{item.status}'")

    ok = apply_queue_item(db, item, user_id=user.id)
    if not ok:
        raise HTTPException(500, "Failed to apply enrichment")

    db.commit()
    return {"status": "approved", "id": item_id}


@router.post("/api/enrichment/queue/{item_id}/reject")
def api_reject_item(
    item_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Reject an enrichment queue item."""
    item = db.get(EnrichmentQueue, item_id)
    if not item:
        raise HTTPException(404, "Queue item not found")
    if item.status != "pending":
        raise HTTPException(400, f"Cannot reject item with status '{item.status}'")

    item.status = "rejected"
    item.reviewed_by_id = user.id
    item.reviewed_at = datetime.now(timezone.utc)
    db.commit()
    return {"status": "rejected", "id": item_id}


@router.post("/api/enrichment/queue/bulk-approve")
def api_bulk_approve(
    body: BulkApproveRequest,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Bulk approve multiple enrichment queue items."""
    from ..services.deep_enrichment_service import apply_queue_item

    # Batch-fetch all items in one query instead of db.get() per item
    items = db.query(EnrichmentQueue).filter(EnrichmentQueue.id.in_(body.ids)).all()
    item_map = {item.id: item for item in items}

    approved = 0
    failed = 0
    for item_id in body.ids:
        item = item_map.get(item_id)
        if item and item.status in ("pending", "low_confidence"):
            ok = apply_queue_item(db, item, user_id=user.id)
            if ok:
                approved += 1
            else:
                failed += 1

    db.commit()
    return {"approved": approved, "failed": failed}


# ── Job endpoints ────────────────────────────────────────────────────


@router.post("/api/enrichment/backfill")
async def api_start_backfill(
    body: BackfillRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Start a backfill enrichment job (admin only)."""
    from ..services.deep_enrichment_service import run_backfill_job

    # Check for already-running jobs
    running = db.query(EnrichmentJob).filter(
        EnrichmentJob.status == "running"
    ).first()
    if running:
        raise HTTPException(409, f"A backfill job is already running (job #{running.id})")

    job_id = await run_backfill_job(
        db, user.id,
        scope={
            "entity_types": body.entity_types,
            "max_items": body.max_items,
            "include_deep_email": body.include_deep_email,
            "lookback_days": body.lookback_days,
        },
    )
    return {"status": "started", "job_id": job_id}


@router.get("/api/enrichment/jobs/{job_id}")
def api_get_job(
    job_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get job progress with percentage."""
    job = db.get(EnrichmentJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    progress = 0.0
    if job.total_items > 0:
        progress = round((job.processed_items / job.total_items) * 100, 1)

    started_by_name = None
    if job.started_by_id:
        starter = db.get(User, job.started_by_id)
        started_by_name = starter.name or starter.email if starter else None

    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "total_items": job.total_items,
        "processed_items": job.processed_items,
        "enriched_items": job.enriched_items,
        "error_count": job.error_count,
        "progress_pct": progress,
        "scope": job.scope,
        "started_by": started_by_name,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_log": (job.error_log or [])[:20],
    }


@router.get("/api/enrichment/jobs")
def api_list_jobs(
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all enrichment jobs."""
    jobs = (
        db.query(EnrichmentJob)
        .order_by(EnrichmentJob.created_at.desc())
        .limit(limit)
        .all()
    )
    results = []
    for job in jobs:
        progress = 0.0
        if job.total_items > 0:
            progress = round((job.processed_items / job.total_items) * 100, 1)

        started_by_name = None
        if job.started_by_id:
            starter = db.get(User, job.started_by_id)
            started_by_name = starter.name or starter.email if starter else None

        results.append({
            "id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "total_items": job.total_items,
            "processed_items": job.processed_items,
            "enriched_items": job.enriched_items,
            "error_count": job.error_count,
            "progress_pct": progress,
            "started_by": started_by_name,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        })
    return {"jobs": results}


@router.post("/api/enrichment/jobs/{job_id}/cancel")
def api_cancel_job(
    job_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Cancel a running job."""
    job = db.get(EnrichmentJob, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "running":
        raise HTTPException(400, f"Cannot cancel job with status '{job.status}'")

    job.status = "cancelled"
    db.commit()
    return {"status": "cancelled", "job_id": job_id}


# ── On-demand enrichment ─────────────────────────────────────────────


class EnrichRequest(BaseModel):
    force: bool = False


@router.post("/api/enrichment/vendor/{vendor_id}")
async def api_enrich_vendor(
    vendor_id: int,
    body: EnrichRequest = EnrichRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger immediate deep enrichment for a vendor."""
    from ..services.deep_enrichment_service import deep_enrich_vendor

    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    result = await deep_enrich_vendor(vendor_id, db, force=body.force)
    return result


@router.post("/api/enrichment/company/{company_id}")
async def api_enrich_company(
    company_id: int,
    body: EnrichRequest = EnrichRequest(),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger immediate deep enrichment for a company."""
    from ..services.deep_enrichment_service import deep_enrich_company

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    result = await deep_enrich_company(company_id, db, force=body.force)
    return result


# ── Stats ────────────────────────────────────────────────────────────


@router.get("/api/enrichment/stats")
def api_enrichment_stats(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Get enrichment statistics — queue counts, coverage, active jobs."""
    from sqlalchemy import func

    queue_stats = (
        db.query(EnrichmentQueue.status, func.count(EnrichmentQueue.id))
        .group_by(EnrichmentQueue.status)
        .all()
    )
    status_counts = {s: c for s, c in queue_stats}

    vendors_total = db.query(func.count(VendorCard.id)).scalar() or 0
    vendors_enriched = db.query(func.count(VendorCard.id)).filter(
        VendorCard.deep_enrichment_at.isnot(None)
    ).scalar() or 0

    companies_total = db.query(func.count(Company.id)).scalar() or 0
    companies_enriched = db.query(func.count(Company.id)).filter(
        Company.deep_enrichment_at.isnot(None)
    ).scalar() or 0

    active_jobs = db.query(func.count(EnrichmentJob.id)).filter(
        EnrichmentJob.status == "running"
    ).scalar() or 0

    return {
        "queue_pending": status_counts.get("pending", 0),
        "queue_approved": status_counts.get("approved", 0),
        "queue_rejected": status_counts.get("rejected", 0),
        "queue_auto_applied": status_counts.get("auto_applied", 0),
        "vendors_enriched": vendors_enriched,
        "vendors_total": vendors_total,
        "companies_enriched": companies_enriched,
        "companies_total": companies_total,
        "active_jobs": active_jobs,
        "vendor_emails": db.query(func.count(VendorContact.id)).filter(
            VendorContact.email.isnot(None)
        ).scalar() or 0,
    }


# ── Email Backfill ────────────────────────────────────────────────────


@router.post("/api/enrichment/backfill-emails")
def api_backfill_emails(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """One-time backfill: recover vendor emails from activity_log, VendorCard.emails, and BrokerBin sightings."""
    from ..vendor_utils import normalize_vendor_name

    now = datetime.now(timezone.utc)
    activity_log_created = 0
    vendor_card_created = 0
    brokerbin_created = 0

    # 1. Activity log orphans — emails in activity_log not in vendor_contacts
    activity_rows = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.contact_email.isnot(None),
            ActivityLog.contact_email != "",
            ActivityLog.vendor_card_id.isnot(None),
        )
        .all()
    )
    for row in activity_rows:
        email = row.contact_email.strip().lower()
        if not email or "@" not in email:
            continue
        existing = (
            db.query(VendorContact)
            .filter_by(vendor_card_id=row.vendor_card_id, email=email)
            .first()
        )
        if existing:
            continue
        vc = VendorContact(
            vendor_card_id=row.vendor_card_id,
            email=email,
            full_name=row.contact_name,
            source="activity_log",
            confidence=55,
            contact_type="individual" if row.contact_name else "company",
        )
        db.add(vc)
        activity_log_created += 1

    # 2. VendorCard.emails consolidation
    cards_with_emails = (
        db.query(VendorCard)
        .filter(VendorCard.emails.isnot(None))
        .all()
    )
    for card in cards_with_emails:
        emails = card.emails or []
        if not isinstance(emails, list):
            continue
        for email in emails:
            email = (email or "").strip().lower()
            if not email or "@" not in email:
                continue
            existing = (
                db.query(VendorContact)
                .filter_by(vendor_card_id=card.id, email=email)
                .first()
            )
            if existing:
                continue
            vc = VendorContact(
                vendor_card_id=card.id,
                email=email,
                source="vendor_card_import",
                confidence=50,
                contact_type="company",
            )
            db.add(vc)
            vendor_card_created += 1

    # 3. BrokerBin sighting re-scan
    bb_sightings = (
        db.query(Sighting)
        .filter(
            Sighting.source_type == "brokerbin",
            Sighting.vendor_email.isnot(None),
            Sighting.vendor_email != "",
        )
        .all()
    )
    for s in bb_sightings:
        email = s.vendor_email.strip().lower()
        if not email or "@" not in email:
            continue
        vn = (s.vendor_name or "").strip()
        if not vn:
            continue
        norm = normalize_vendor_name(vn)
        if not norm:
            continue
        card = db.query(VendorCard).filter_by(normalized_name=norm).first()
        if not card:
            continue
        existing = (
            db.query(VendorContact)
            .filter_by(vendor_card_id=card.id, email=email)
            .first()
        )
        if existing:
            continue
        vc = VendorContact(
            vendor_card_id=card.id,
            email=email,
            source="brokerbin",
            confidence=60,
            contact_type="company",
        )
        db.add(vc)
        brokerbin_created += 1

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        log.error("Email backfill commit failed: %s", e)
        raise HTTPException(500, f"Backfill failed: {e}")

    total = activity_log_created + vendor_card_created + brokerbin_created
    log.info(
        "Email backfill complete: %d total (%d activity_log, %d vendor_card, %d brokerbin)",
        total, activity_log_created, vendor_card_created, brokerbin_created,
    )
    return {
        "activity_log_created": activity_log_created,
        "vendor_card_created": vendor_card_created,
        "brokerbin_created": brokerbin_created,
        "total_created": total,
    }


# ── M365 Status & Deep Scan ──────────────────────────────────────────


@router.get("/api/enrichment/m365-status")
def api_m365_status(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get M365 connection status for all users."""
    users = db.query(User).filter(User.is_active == True).order_by(User.name).all()  # noqa: E712
    return {
        "users": [
            {
                "id": u.id,
                "name": u.name or u.email,
                "email": u.email,
                "m365_connected": bool(u.m365_connected),
                "error_reason": u.m365_error_reason,
                "last_inbox_scan": u.last_inbox_scan.isoformat() if u.last_inbox_scan else None,
                "last_deep_scan": u.last_deep_email_scan.isoformat() if u.last_deep_email_scan else None,
            }
            for u in users
        ]
    }


@router.post("/api/enrichment/deep-email-scan/{user_id}")
async def api_deep_email_scan(
    user_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Trigger a deep inbox scan for a specific user to extract vendor contacts."""
    from ..connectors.email_mining import EmailMiner
    from ..scheduler import get_valid_token
    from ..vendor_utils import normalize_vendor_name, merge_emails_into_card

    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(404, "User not found")
    if not target_user.m365_connected:
        raise HTTPException(400, "User does not have M365 connected")

    fresh_token = await get_valid_token(target_user, db) or target_user.access_token
    if not fresh_token:
        raise HTTPException(400, "No valid access token for user")

    miner = EmailMiner(fresh_token, db=db, user_id=target_user.id)
    results = await miner.deep_scan_inbox(lookback_days=365, max_messages=2000)

    contacts_created = 0
    per_domain = results.get("per_domain", {})
    for domain, data in per_domain.items():
        emails = data.get("emails", [])
        if not emails:
            continue

        # Try to find a vendor card matching this domain
        card = db.query(VendorCard).filter(
            (VendorCard.domain == domain) |
            (VendorCard.website.ilike(f"%{domain}%"))
        ).first()

        if not card:
            # Try matching by normalized name from domain
            domain_name = domain.split(".")[0] if domain else ""
            if domain_name:
                norm = normalize_vendor_name(domain_name)
                card = db.query(VendorCard).filter_by(normalized_name=norm).first()

        if not card:
            continue

        merge_emails_into_card(card, emails)

        for email in emails:
            email_lower = email.strip().lower()
            if not email_lower or "@" not in email_lower:
                continue
            existing = (
                db.query(VendorContact)
                .filter_by(vendor_card_id=card.id, email=email_lower)
                .first()
            )
            if existing:
                continue
            vc = VendorContact(
                vendor_card_id=card.id,
                email=email_lower,
                source="email_mining_deep",
                confidence=70,
                contact_type="company",
            )
            db.add(vc)
            contacts_created += 1

    target_user.last_deep_email_scan = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        log.error("Deep scan commit failed for user %s: %s", target_user.email, e)
        raise HTTPException(500, f"Deep scan failed: {e}")

    log.info("Deep email scan for %s: %d contacts created", target_user.email, contacts_created)
    return {
        "messages_scanned": results.get("messages_scanned", 0),
        "contacts_created": contacts_created,
    }


# ── Website Scraping ─────────────────────────────────────────────────


class ScrapeRequest(BaseModel):
    max_vendors: int = 500


@router.post("/api/enrichment/scrape-websites")
async def api_scrape_websites(
    body: ScrapeRequest = ScrapeRequest(),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Scrape vendor websites for contact emails."""
    from ..services.website_scraper import scrape_vendor_websites

    result = await scrape_vendor_websites(db, max_vendors=body.max_vendors)
    return result

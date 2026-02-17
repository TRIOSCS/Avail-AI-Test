"""Enrichment API — review queue, backfill jobs, on-demand enrichment, stats."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_admin, require_user
from ..models import (
    EnrichmentJob,
    EnrichmentQueue,
    User,
    VendorCard,
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

    approved = 0
    failed = 0
    for item_id in body.ids:
        item = db.get(EnrichmentQueue, item_id)
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


@router.post("/api/enrichment/vendor/{vendor_id}")
async def api_enrich_vendor(
    vendor_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger immediate deep enrichment for a vendor."""
    from ..services.deep_enrichment_service import deep_enrich_vendor

    card = db.get(VendorCard, vendor_id)
    if not card:
        raise HTTPException(404, "Vendor not found")

    result = await deep_enrich_vendor(vendor_id, db)
    return result


@router.post("/api/enrichment/company/{company_id}")
async def api_enrich_company(
    company_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Trigger immediate deep enrichment for a company."""
    from ..services.deep_enrichment_service import deep_enrich_company

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, "Company not found")

    result = await deep_enrich_company(company_id, db)
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
    }

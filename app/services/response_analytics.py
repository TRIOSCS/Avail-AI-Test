"""Response Analytics Service — compute response time metrics and vendor email health.

Pairs outbound RFQs with inbound replies to calculate response metrics.
Computes a composite 0-100 email health score per vendor.

Scoring weights:
  - Response rate (30%): fraction of RFQs that received a reply
  - Avg response time (25%): lower is better, 0-168h scale
  - Quote quality (20%): fraction of replies that included pricing
  - OOO frequency (10%): how often contacts are OOO
  - Thread resolution (15%): fraction of threads with "closed" or "quoted" status

Called by: scheduler.py (planned), routers/emails.py (dashboard)
Depends on: models (VendorCard, VendorResponse, EmailIntelligence, ActivityLog)
"""

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

# ── Health score weights ─────────────────────────────────────────────
W_RESPONSE_RATE = 0.30
W_RESPONSE_TIME = 0.25
W_QUOTE_QUALITY = 0.20
W_OOO_FREQUENCY = 0.10
W_THREAD_RESOLUTION = 0.15

# Response time scoring: <=4h = 100, >=168h = 0
RESPONSE_IDEAL_HOURS = 4.0
RESPONSE_MAX_HOURS = 168.0


def compute_vendor_response_metrics(
    db: Session, vendor_card_id: int, lookback_days: int = 90
) -> dict:
    """Compute response time metrics for a single vendor.

    Pairs outbound RFQ activity with inbound VendorResponse records
    by vendor_card_id or vendor domain.

    Returns:
        {
            avg_response_hours: float | None,
            median_response_hours: float | None,
            response_rate: float,      # 0.0-1.0
            response_count: int,
            outreach_count: int,
            quote_quality_rate: float,  # 0.0-1.0
        }
    """
    from app.models import ActivityLog, VendorCard, VendorResponse

    vendor = db.get(VendorCard, vendor_card_id)
    if not vendor:
        return _empty_metrics()

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # Count outbound RFQs to this vendor
    outreach_count = (
        db.query(func.count(ActivityLog.id))
        .filter(
            ActivityLog.vendor_card_id == vendor_card_id,
            ActivityLog.activity_type.in_(["rfq_sent", "email_sent"]),
            ActivityLog.created_at >= cutoff,
        )
        .scalar()
    ) or 0

    # Also count from total_outreach on the card if no activity log entries
    if outreach_count == 0 and vendor.total_outreach:
        outreach_count = vendor.total_outreach

    # Get vendor responses
    domain = vendor.domain or ""
    response_query = db.query(VendorResponse).filter(
        VendorResponse.received_at >= cutoff,
    )

    if domain:
        response_query = response_query.filter(
            VendorResponse.vendor_email.ilike(f"%@{domain}")
        )
    else:
        response_query = response_query.filter(
            VendorResponse.vendor_name == vendor.display_name
        )

    responses = response_query.all()
    response_count = len(responses)

    # Response rate
    response_rate = 0.0
    if outreach_count > 0:
        response_rate = min(1.0, response_count / outreach_count)

    # Response times (from received_at - created_at on VendorResponse)
    response_hours = []
    quotes_with_pricing = 0

    for r in responses:
        if r.received_at and r.created_at:
            diff = (r.received_at - r.created_at).total_seconds() / 3600
            if 0 < diff < RESPONSE_MAX_HOURS * 2:
                response_hours.append(diff)

        # Check if response had pricing data
        if r.parsed_data and isinstance(r.parsed_data, dict):
            if r.parsed_data.get("quotes") or r.parsed_data.get("line_items"):
                quotes_with_pricing += 1

    avg_hours = None
    median_hours = None
    if response_hours:
        response_hours.sort()
        avg_hours = sum(response_hours) / len(response_hours)
        mid = len(response_hours) // 2
        if len(response_hours) % 2 == 0 and len(response_hours) >= 2:
            median_hours = (response_hours[mid - 1] + response_hours[mid]) / 2
        else:
            median_hours = response_hours[mid]

    quote_quality_rate = 0.0
    if response_count > 0:
        quote_quality_rate = quotes_with_pricing / response_count

    return {
        "avg_response_hours": round(avg_hours, 1) if avg_hours is not None else None,
        "median_response_hours": round(median_hours, 1) if median_hours is not None else None,
        "response_rate": round(response_rate, 3),
        "response_count": response_count,
        "outreach_count": outreach_count,
        "quote_quality_rate": round(quote_quality_rate, 3),
    }


def _empty_metrics() -> dict:
    return {
        "avg_response_hours": None,
        "median_response_hours": None,
        "response_rate": 0.0,
        "response_count": 0,
        "outreach_count": 0,
        "quote_quality_rate": 0.0,
    }


def compute_email_health_score(
    db: Session, vendor_card_id: int, lookback_days: int = 90
) -> dict:
    """Compute composite 0-100 email health score for a vendor.

    Components:
      - Response rate (30%): outreach → reply ratio
      - Avg response time (25%): faster = better
      - Quote quality (20%): fraction of replies with pricing
      - OOO frequency (10%): lower OOO ratio = better
      - Thread resolution (15%): fraction of resolved threads

    Returns:
        {
            email_health_score: float,
            response_rate_score: float,
            response_time_score: float,
            quote_quality_score: float,
            ooo_score: float,
            thread_resolution_score: float,
            metrics: dict,
        }
    """
    from app.models import EmailIntelligence, VendorCard, VendorContact

    metrics = compute_vendor_response_metrics(db, vendor_card_id, lookback_days)

    # Response rate score (0-100)
    response_rate_score = min(100.0, metrics["response_rate"] * 100.0)

    # Response time score (0-100): <=4h=100, >=168h=0
    if metrics["avg_response_hours"] is not None:
        hours = metrics["avg_response_hours"]
        if hours <= RESPONSE_IDEAL_HOURS:
            response_time_score = 100.0
        elif hours >= RESPONSE_MAX_HOURS:
            response_time_score = 0.0
        else:
            response_time_score = max(
                0.0,
                100.0 * (1.0 - (hours - RESPONSE_IDEAL_HOURS) / (RESPONSE_MAX_HOURS - RESPONSE_IDEAL_HOURS)),
            )
    else:
        response_time_score = 50.0  # Unknown defaults to neutral

    # Quote quality score (0-100)
    quote_quality_score = min(100.0, metrics["quote_quality_rate"] * 100.0)

    # OOO frequency score (0-100): fewer OOO contacts = better
    total_contacts = (
        db.query(func.count(VendorContact.id))
        .filter(VendorContact.vendor_card_id == vendor_card_id)
        .scalar()
    ) or 0

    ooo_contacts = 0
    if total_contacts > 0:
        ooo_contacts = (
            db.query(func.count(VendorContact.id))
            .filter(
                VendorContact.vendor_card_id == vendor_card_id,
                VendorContact.is_ooo.is_(True),
            )
            .scalar()
        ) or 0

    if total_contacts > 0:
        ooo_ratio = ooo_contacts / total_contacts
        ooo_score = max(0.0, 100.0 * (1.0 - ooo_ratio))
    else:
        ooo_score = 100.0  # No contacts = no OOO issue

    # Thread resolution score (0-100)
    vendor = db.get(VendorCard, vendor_card_id)
    domain = (vendor.domain or "") if vendor else ""

    thread_resolution_score = 50.0  # Default neutral
    if domain:
        total_threads = (
            db.query(func.count(EmailIntelligence.id))
            .filter(
                EmailIntelligence.sender_domain == domain,
                EmailIntelligence.thread_summary.isnot(None),
            )
            .scalar()
        ) or 0

        if total_threads > 0:
            # Count threads with "closed" or "quoted" status
            # thread_summary is JSON with thread_status field
            resolved = 0
            thread_records = (
                db.query(EmailIntelligence.thread_summary)
                .filter(
                    EmailIntelligence.sender_domain == domain,
                    EmailIntelligence.thread_summary.isnot(None),
                )
                .all()
            )
            for (summary,) in thread_records:
                if isinstance(summary, dict):
                    status = summary.get("thread_status", "")
                    if status in ("closed", "quoted"):
                        resolved += 1
            thread_resolution_score = min(100.0, (resolved / total_threads) * 100.0)

    # Weighted composite
    score = (
        W_RESPONSE_RATE * response_rate_score
        + W_RESPONSE_TIME * response_time_score
        + W_QUOTE_QUALITY * quote_quality_score
        + W_OOO_FREQUENCY * ooo_score
        + W_THREAD_RESOLUTION * thread_resolution_score
    )

    return {
        "email_health_score": round(score, 1),
        "response_rate_score": round(response_rate_score, 1),
        "response_time_score": round(response_time_score, 1),
        "quote_quality_score": round(quote_quality_score, 1),
        "ooo_score": round(ooo_score, 1),
        "thread_resolution_score": round(thread_resolution_score, 1),
        "metrics": metrics,
    }


def update_vendor_email_health(
    db: Session, vendor_card_id: int, lookback_days: int = 90
) -> dict | None:
    """Compute and persist email health score on VendorCard.

    Returns the health dict or None if vendor not found.
    """
    from app.models import VendorCard

    vendor = db.get(VendorCard, vendor_card_id)
    if not vendor:
        return None

    health = compute_email_health_score(db, vendor_card_id, lookback_days)

    vendor.email_health_score = health["email_health_score"]
    vendor.email_health_computed_at = datetime.now(timezone.utc)
    vendor.response_rate = health["metrics"]["response_rate"]
    vendor.quote_quality_rate = health["metrics"]["quote_quality_rate"]

    # Also update avg_response_hours for contact intelligence integration
    if health["metrics"]["avg_response_hours"] is not None:
        vendor.avg_response_hours = health["metrics"]["avg_response_hours"]

    db.flush()
    return health


def batch_update_email_health(
    db: Session, lookback_days: int = 90, limit: int = 500
) -> dict:
    """Batch recompute email health scores for active vendors.

    Prioritizes vendors with recent activity or stale scores.
    Returns: {updated: int, errors: int}
    """
    from app.models import VendorCard

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    vendors = (
        db.query(VendorCard.id)
        .filter(
            VendorCard.last_contact_at >= cutoff,
        )
        .order_by(VendorCard.email_health_computed_at.asc().nullsfirst())
        .limit(limit)
        .all()
    )

    updated = 0
    errors = 0

    for (vid,) in vendors:
        try:
            update_vendor_email_health(db, vid, lookback_days)
            updated += 1
        except Exception as e:
            logger.warning("Email health update failed for vendor %d: %s", vid, e)
            errors += 1

    if updated:
        try:
            db.commit()
        except Exception as e:
            logger.warning("Batch email health commit failed: %s", e)
            db.rollback()

    return {"updated": updated, "errors": errors}


def get_email_intelligence_dashboard(
    db: Session, user_id: int, days: int = 7
) -> dict:
    """Aggregated email intelligence dashboard data.

    Returns:
        {
            emails_scanned_7d: int,
            offers_detected_7d: int,
            stock_lists_7d: int,
            ooo_vendors: int,
            avg_response_hours: float | None,
            response_rate: float | None,
            top_vendors: [{vendor_name, email_health_score, response_rate}],
            recent_offers: [{subject, sender, confidence, received_at}],
            pending_review: int,
        }
    """
    from app.models import EmailIntelligence, VendorCard, VendorContact

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Emails scanned in window
    emails_scanned = (
        db.query(func.count(EmailIntelligence.id))
        .filter(
            EmailIntelligence.user_id == user_id,
            EmailIntelligence.created_at >= cutoff,
        )
        .scalar()
    ) or 0

    # Offers detected
    offers_detected = (
        db.query(func.count(EmailIntelligence.id))
        .filter(
            EmailIntelligence.user_id == user_id,
            EmailIntelligence.classification.in_(["offer", "quote_reply"]),
            EmailIntelligence.created_at >= cutoff,
        )
        .scalar()
    ) or 0

    # Stock lists
    stock_lists = (
        db.query(func.count(EmailIntelligence.id))
        .filter(
            EmailIntelligence.user_id == user_id,
            EmailIntelligence.classification == "stock_list",
            EmailIntelligence.created_at >= cutoff,
        )
        .scalar()
    ) or 0

    # OOO vendors
    ooo_vendors = (
        db.query(func.count(VendorContact.id))
        .filter(VendorContact.is_ooo.is_(True))
        .scalar()
    ) or 0

    # Average response hours across all vendors with health scores
    avg_resp = (
        db.query(func.avg(VendorCard.avg_response_hours))
        .filter(VendorCard.avg_response_hours.isnot(None))
        .scalar()
    )

    # Average response rate
    avg_rate = (
        db.query(func.avg(VendorCard.response_rate))
        .filter(VendorCard.response_rate.isnot(None))
        .scalar()
    )

    # Top vendors by email health
    top_vendors_q = (
        db.query(VendorCard)
        .filter(VendorCard.email_health_score.isnot(None))
        .order_by(VendorCard.email_health_score.desc())
        .limit(10)
        .all()
    )
    top_vendors = [
        {
            "id": v.id,
            "vendor_name": v.display_name,
            "email_health_score": v.email_health_score,
            "response_rate": v.response_rate,
        }
        for v in top_vendors_q
    ]

    # Recent offers
    recent_offers_q = (
        db.query(EmailIntelligence)
        .filter(
            EmailIntelligence.user_id == user_id,
            EmailIntelligence.classification.in_(["offer", "quote_reply"]),
            EmailIntelligence.created_at >= cutoff,
        )
        .order_by(EmailIntelligence.created_at.desc())
        .limit(10)
        .all()
    )
    recent_offers = [
        {
            "id": r.id,
            "subject": r.subject,
            "sender_email": r.sender_email,
            "confidence": r.confidence,
            "received_at": r.received_at.isoformat() if r.received_at else None,
            "auto_applied": r.auto_applied,
        }
        for r in recent_offers_q
    ]

    # Pending review count
    pending_review = (
        db.query(func.count(EmailIntelligence.id))
        .filter(
            EmailIntelligence.user_id == user_id,
            EmailIntelligence.needs_review.is_(True),
        )
        .scalar()
    ) or 0

    return {
        "emails_scanned_7d": emails_scanned,
        "offers_detected_7d": offers_detected,
        "stock_lists_7d": stock_lists,
        "ooo_vendors": ooo_vendors,
        "avg_response_hours": round(avg_resp, 1) if avg_resp else None,
        "response_rate": round(avg_rate, 3) if avg_rate else None,
        "top_vendors": top_vendors,
        "recent_offers": recent_offers,
        "pending_review": pending_review,
    }

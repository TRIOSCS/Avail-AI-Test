"""
command_center.py — Command Center API endpoint

Provides aggregated action items for the dashboard:
- Stale vendor RFQs (no response after 48h)
- Pending quotes (sent > 5 days ago)
- Offers needing review
- Today's vendor responses
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user

router = APIRouter(prefix="/api/command-center", tags=["command-center"])


@router.get("/actions")
def get_command_center_actions(
    db: Session = Depends(get_db),
    user=Depends(require_user),
):
    from ..models.offers import Contact, Offer, VendorResponse
    from ..models.quotes import Quote

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_48h = now - timedelta(hours=48)
    cutoff_5d = now - timedelta(days=5)

    # Stale RFQs: status="sent", created > 48h ago, no response yet
    stale_rfqs = (
        db.query(Contact)
        .filter(
            Contact.status == "sent",
            Contact.created_at < cutoff_48h,
        )
        .order_by(Contact.created_at.asc())
        .limit(20)
        .all()
    )

    # Pending quotes: status="sent", sent > 5 days ago
    pending_quotes = (
        db.query(Quote)
        .filter(
            Quote.status == "sent",
            Quote.sent_at < cutoff_5d,
            Quote.result.is_(None),
        )
        .order_by(Quote.sent_at.asc())
        .limit(20)
        .all()
    )

    # Offers needing review
    pending_reviews = (
        db.query(Offer).filter(Offer.status == "needs_review").order_by(Offer.created_at.desc()).limit(20).all()
    )

    # Today's vendor responses
    today_responses = (
        db.query(VendorResponse)
        .filter(VendorResponse.created_at >= today_start)
        .order_by(VendorResponse.created_at.desc())
        .limit(20)
        .all()
    )

    return {
        "stale_rfqs": [
            {
                "id": c.id,
                "requisition_id": c.requisition_id,
                "vendor_name": c.vendor_name,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in stale_rfqs
        ],
        "pending_quotes": [
            {
                "id": q.id,
                "requisition_id": q.requisition_id,
                "quote_number": q.quote_number,
                "sent_at": q.sent_at.isoformat() if q.sent_at else None,
                "days_pending": (now - (q.sent_at if q.sent_at.tzinfo else q.sent_at.replace(tzinfo=timezone.utc))).days
                if q.sent_at
                else None,
            }
            for q in pending_quotes
        ],
        "pending_reviews": [
            {
                "id": o.id,
                "requisition_id": o.requisition_id,
                "vendor_name": o.vendor_name,
                "mpn": o.mpn,
            }
            for o in pending_reviews
        ],
        "today_responses": [
            {
                "id": vr.id,
                "vendor_name": vr.vendor_name,
                "subject": vr.subject,
                "confidence": vr.confidence,
                "requisition_id": vr.requisition_id,
            }
            for vr in today_responses
        ],
    }

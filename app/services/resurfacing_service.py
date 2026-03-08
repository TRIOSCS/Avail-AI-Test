"""Cross-customer MPN resurfacing service — inline hints wherever MPNs appear.

Pure SQL, no AI. Sub-50ms for batch of 20 MPNs.

For each MPN, generates a one-line hint from:
1. Latest offer price + vendor + date
2. Cross-req matches (other open reqs with same MPN)
3. High-confidence knowledge facts (lead time, EOL)

Called by: routers/knowledge.py (batch hints endpoint)
Depends on: models (Offer, Requisition, Requirement, KnowledgeEntry)
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.knowledge import KnowledgeEntry
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition


def get_mpn_hints(
    mpns: list[str],
    db: Session,
    exclude_req_id: int | None = None,
) -> dict[str, str | None]:
    """Return a dict mapping each MPN to a one-line hint string, or None.

    Args:
        mpns: list of MPN strings to look up
        db: SQLAlchemy session
        exclude_req_id: optional requisition ID to exclude from results

    Returns:
        dict keyed by MPN with hint string or None
    """
    if not mpns:
        return {}

    result: dict[str, str | None] = {}
    for mpn in mpns:
        try:
            result[mpn] = _build_hint(mpn, db, exclude_req_id)
        except Exception:
            logger.opt(exception=True).warning("Failed to build hint for MPN {}", mpn)
            result[mpn] = None
    return result


def _build_hint(
    mpn: str,
    db: Session,
    exclude_req_id: int | None,
) -> str | None:
    """Build a single hint string for the given MPN.

    Priority order:
    1. Latest offer with unit_price > 0
    2. Cross-req matches (open/in_progress/quoting)
    3. High-confidence knowledge facts
    """
    # 1. Latest offer
    hint = _offer_hint(mpn, db, exclude_req_id)
    if hint:
        return hint

    # 2. Cross-req matches
    hint = _cross_req_hint(mpn, db, exclude_req_id)
    if hint:
        return hint

    # 3. Knowledge facts
    hint = _knowledge_hint(mpn, db)
    if hint:
        return hint

    return None


def _offer_hint(mpn: str, db: Session, exclude_req_id: int | None) -> str | None:
    """Find latest offer with unit_price > 0 for this MPN."""
    query = db.query(Offer).filter(Offer.mpn == mpn, Offer.unit_price > 0).order_by(Offer.created_at.desc())
    if exclude_req_id is not None:
        query = query.filter(Offer.requisition_id != exclude_req_id)

    offer = query.first()
    if not offer:
        return None

    price_str = "${:.2f}".format(float(offer.unit_price))
    vendor = offer.vendor_name or "unknown vendor"
    age = _format_age(offer.created_at)
    return "Last quoted {} from {}, {}".format(price_str, vendor, age)


def _cross_req_hint(mpn: str, db: Session, exclude_req_id: int | None) -> str | None:
    """Find other open requisitions containing this MPN."""
    open_statuses = ("active", "in_progress", "quoting")
    query = (
        db.query(Requisition.id)
        .join(Requirement, Requirement.requisition_id == Requisition.id)
        .filter(
            Requirement.primary_mpn == mpn,
            Requisition.status.in_(open_statuses),
        )
    )
    if exclude_req_id is not None:
        query = query.filter(Requisition.id != exclude_req_id)

    rows = query.distinct().limit(5).all()
    if not rows:
        return None

    ids = ["#{}".format(r[0]) for r in rows]
    return "Also on Req {}".format(", ".join(ids))


def _knowledge_hint(mpn: str, db: Session) -> str | None:
    """Find high-confidence knowledge facts for this MPN."""
    now = datetime.now(timezone.utc)
    entry = (
        db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.mpn == mpn,
            KnowledgeEntry.confidence >= 0.7,
            and_(
                # Not expired
                (KnowledgeEntry.expires_at.is_(None)) | (KnowledgeEntry.expires_at > now)
            ),
        )
        .order_by(KnowledgeEntry.confidence.desc())
        .first()
    )
    if not entry:
        return None

    return entry.content


def _format_age(dt: datetime | None) -> str:
    """Format a datetime as a human-readable age string."""
    if dt is None:
        return "unknown date"

    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    delta = now - dt
    days = delta.days

    if days == 0:
        return "today"
    if days == 1:
        return "1d ago"
    if days < 60:
        return "{}d ago".format(days)

    months = days // 30
    if months == 1:
        return "1mo ago"
    return "{}mo ago".format(months)

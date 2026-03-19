"""proactive_helpers.py — Shared helpers for proactive matching.

Deduplicates do-not-offer checks, throttle checks, and batch query patterns
used across proactive_matching.py, proactive_service.py, and htmx_views.py.

Called by: services/proactive_matching.py, services/proactive_service.py, routers/htmx_views.py
Depends on: models/intelligence.py, config.py
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models.intelligence import ProactiveDoNotOffer, ProactiveThrottle


def is_do_not_offer(db: Session, mpn: str, company_id: int) -> bool:
    """Check if MPN is permanently suppressed for a company."""
    mpn_upper = mpn.strip().upper()
    return (
        db.query(ProactiveDoNotOffer.id)
        .filter(
            ProactiveDoNotOffer.mpn == mpn_upper,
            ProactiveDoNotOffer.company_id == company_id,
        )
        .first()
        is not None
    )


def is_throttled(db: Session, mpn: str, site_id: int, days: int | None = None) -> bool:
    """Check if MPN was recently offered to a customer site."""
    mpn_upper = mpn.strip().upper()
    throttle_days = days or settings.proactive_throttle_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=throttle_days)
    return (
        db.query(ProactiveThrottle.id)
        .filter(
            ProactiveThrottle.mpn == mpn_upper,
            ProactiveThrottle.customer_site_id == site_id,
            ProactiveThrottle.last_offered_at > cutoff,
        )
        .first()
        is not None
    )


def build_batch_dno_set(db: Session, mpn: str, company_ids: set[int]) -> set[int]:
    """Batch-load do-not-offer company IDs for a given MPN.

    Returns set of company_ids that have this MPN suppressed.
    """
    if not company_ids:
        return set()
    mpn_upper = mpn.strip().upper()
    return {
        row[0]
        for row in db.query(ProactiveDoNotOffer.company_id)
        .filter(
            ProactiveDoNotOffer.mpn == mpn_upper,
            ProactiveDoNotOffer.company_id.in_(company_ids),
        )
        .all()
    }


def build_batch_throttle_set(db: Session, mpn: str, site_ids: set[int], days: int | None = None) -> set[int]:
    """Batch-load throttled site IDs for a given MPN.

    Returns set of customer_site_ids where this MPN was recently offered.
    """
    if not site_ids:
        return set()
    mpn_upper = mpn.strip().upper()
    throttle_days = days or settings.proactive_throttle_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=throttle_days)
    return {
        row[0]
        for row in db.query(ProactiveThrottle.customer_site_id)
        .filter(
            ProactiveThrottle.mpn == mpn_upper,
            ProactiveThrottle.customer_site_id.in_(site_ids),
            ProactiveThrottle.last_offered_at > cutoff,
        )
        .all()
    }

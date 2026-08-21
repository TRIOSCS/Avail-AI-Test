"""proactive_helpers.py — Shared helpers for proactive matching.

Deduplicates do-not-offer checks, throttle checks, and batch query patterns
used across proactive_matching.py, proactive_service.py, and htmx_views.py.

Called by: services/proactive_matching.py, services/proactive_service.py, routers/htmx_views.py
Depends on: models/intelligence.py, config.py
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ..config import settings
from ..models.intelligence import ProactiveDoNotOffer, ProactiveThrottle


def _throttle_cutoff(days: int | None) -> datetime:
    """Earliest last_offered_at that still counts as throttled (now - throttle window)."""
    throttle_days = days or settings.proactive_throttle_days
    return datetime.now(UTC) - timedelta(days=throttle_days)


def is_do_not_offer(db: Session, mpn: str, company_id: int) -> bool:
    """Check if MPN is permanently suppressed for a company."""
    return company_id in build_batch_dno_set_multi(db, {mpn}, {company_id})


def is_throttled(db: Session, mpn: str, site_id: int, days: int | None = None) -> bool:
    """Check if MPN was recently offered to a customer site."""
    return site_id in build_batch_throttle_set_multi(db, {mpn}, {site_id}, days)


def build_batch_dno_set_multi(db: Session, mpns: set[str], company_ids: set[int]) -> set[int]:
    """build_batch_dno_set across every spelling of an equivalence class."""
    if not company_ids or not mpns:
        return set()
    return {
        row[0]
        for row in db.query(ProactiveDoNotOffer.company_id)
        .filter(
            ProactiveDoNotOffer.mpn.in_({m.strip().upper() for m in mpns}),
            ProactiveDoNotOffer.company_id.in_(company_ids),
        )
        .all()
    }


def build_batch_throttle_set_multi(
    db: Session, mpns: set[str], site_ids: set[int], days: int | None = None
) -> set[int]:
    """build_batch_throttle_set across every spelling of an equivalence class."""
    if not site_ids or not mpns:
        return set()
    cutoff = _throttle_cutoff(days)
    return {
        row[0]
        for row in db.query(ProactiveThrottle.customer_site_id)
        .filter(
            ProactiveThrottle.mpn.in_({m.strip().upper() for m in mpns}),
            ProactiveThrottle.customer_site_id.in_(site_ids),
            ProactiveThrottle.last_offered_at > cutoff,
        )
        .all()
    }


def build_batch_dno_set(db: Session, mpn: str, company_ids: set[int]) -> set[int]:
    """Single-MPN form of :func:`build_batch_dno_set_multi`."""
    return build_batch_dno_set_multi(db, {mpn}, company_ids)


def build_batch_throttle_set(db: Session, mpn: str, site_ids: set[int], days: int | None = None) -> set[int]:
    """Single-MPN form of :func:`build_batch_throttle_set_multi`."""
    return build_batch_throttle_set_multi(db, {mpn}, site_ids, days)

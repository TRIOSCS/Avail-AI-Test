"""Lifecycle sweep job — checks active parts for EOL/obsolete status.

Runs weekly to re-enrich parts marked 'active' or with unknown lifecycle,
updating their lifecycle_status via the AI enrichment pipeline.

Called by: scheduler via register_lifecycle_jobs()
Depends on: MaterialCard, material_enrichment_service
"""

from loguru import logger
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models import MaterialCard


def get_cards_for_lifecycle_check(
    db: Session,
    *,
    limit: int = 200,
) -> list[MaterialCard]:
    """Get cards that need lifecycle status verification.

    Returns active or unknown-status cards, oldest-checked first.
    """
    return (
        db.query(MaterialCard)
        .filter(
            MaterialCard.deleted_at.is_(None),
            or_(
                MaterialCard.lifecycle_status == "active",
                MaterialCard.lifecycle_status.is_(None),
            ),
        )
        .order_by(MaterialCard.enriched_at.asc().nullsfirst())
        .limit(limit)
        .all()
    )


def register_lifecycle_jobs(scheduler, settings):
    """Register lifecycle sweep as a weekly job."""
    logger.info("Lifecycle sweep job DISABLED (AI-only enrichment removed)")

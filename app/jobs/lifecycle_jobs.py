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
from ..scheduler import _traced_job


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
    # Disabled — lifecycle sweep uses AI-only enrichment which produces
    # hallucinated data. Rebuild with real connector data before re-enabling.
    # scheduler.add_job(
    #     _job_lifecycle_sweep,
    #     CronTrigger(day_of_week="sun", hour=2, minute=0),
    #     id="lifecycle_sweep",
    #     name="Weekly lifecycle status sweep",
    #     replace_existing=True,
    # )
    logger.info("Lifecycle sweep job DISABLED (AI-only enrichment removed)")


@_traced_job
async def _job_lifecycle_sweep():
    """Check lifecycle status on active/unknown parts via enrichment."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        cards = get_cards_for_lifecycle_check(db)
        if not cards:
            logger.info("lifecycle_sweep: no cards to check")
            return

        card_ids = [c.id for c in cards]
        logger.info("lifecycle_sweep: checking %d cards", len(card_ids))

        from ..services.material_enrichment_service import enrich_material_cards

        stats = await enrich_material_cards(card_ids, db)
        logger.info("lifecycle_sweep: %s", stats)
    except Exception:
        logger.exception("lifecycle_sweep failed")
        db.rollback()
        raise
    finally:
        db.close()

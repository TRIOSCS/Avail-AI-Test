"""Backfill quotes.source = 'proactive' for quotes linked via
ProactiveOffer.converted_quote_id.

Called by: ops, once after deploy — `docker compose exec app python -m app.management.backfill_quote_source`.
Depends on: ProactiveOffer.converted_quote_id, Quote.source (migration 113_quote_source). Idempotent:
only touches rows where source IS NULL.
"""

from loguru import logger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.intelligence import ProactiveOffer
from app.models.quotes import Quote


def backfill(db: Session) -> int:
    """Set quotes.source = 'proactive' for quotes linked via
    ProactiveOffer.converted_quote_id.

    Returns the number of quotes updated.
    """
    proactive_quote_ids = (
        db.query(ProactiveOffer.converted_quote_id)
        .filter(ProactiveOffer.converted_quote_id.isnot(None))
        .scalar_subquery()
    )
    updated = db.query(Quote).filter(Quote.id.in_(proactive_quote_ids), Quote.source.is_(None)).all()
    for quote in updated:
        quote.source = "proactive"  # type: ignore[assignment, unused-ignore]  # instrumented attr write (legacy Column model)
    db.commit()
    logger.info("QUOTE_SOURCE backfill: updated {} quotes to source='proactive'", len(updated))
    return len(updated)


if __name__ == "__main__":
    db = SessionLocal()
    try:
        backfill(db)
    finally:
        db.close()

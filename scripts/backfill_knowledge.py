"""One-time backfill: seed the Knowledge Ledger from existing quotes and offers.

Usage: docker compose exec app python scripts/backfill_knowledge.py
"""

import sys

sys.path.insert(0, "/app")

from loguru import logger

from app.database import SessionLocal
from app.models.knowledge import KnowledgeEntry
from app.models.offers import Offer
from app.models.quotes import Quote
from app.services.knowledge_service import capture_offer_fact, capture_quote_fact


def backfill():
    db = SessionLocal()
    try:
        # Check if already backfilled
        existing = db.query(KnowledgeEntry).filter(KnowledgeEntry.source == "system").count()
        if existing > 100:
            logger.info("Already have {} system entries — skipping backfill", existing)
            return

        # Backfill from quotes
        quotes = db.query(Quote).order_by(Quote.created_at.desc()).limit(500).all()
        q_count = 0
        for q in quotes:
            try:
                entry = capture_quote_fact(db, quote=q, user_id=q.created_by_id or 0)
                if entry:
                    q_count += 1
            except Exception as e:
                logger.warning("Quote backfill failed for {}: {}", q.id, e)

        # Backfill from offers
        offers = db.query(Offer).order_by(Offer.created_at.desc()).limit(1000).all()
        o_count = 0
        for o in offers:
            try:
                entry = capture_offer_fact(db, offer=o)
                if entry:
                    o_count += 1
            except Exception as e:
                logger.warning("Offer backfill failed for {}: {}", o.id, e)

        logger.info("Backfill complete: {} quote facts, {} offer facts", q_count, o_count)
    except Exception as e:
        logger.error("Backfill failed: {}", e)
    finally:
        db.close()


if __name__ == "__main__":
    backfill()

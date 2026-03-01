"""Backfill existing requirements into the ICsource search queue.

One-time script to enqueue all existing requirements with MPNs that
haven't been searched on ICsource yet.

Usage: PYTHONPATH=/root/availai python scripts/ics_backfill.py [--limit N]

Called by: manual invocation
Depends on: database, ics_worker.queue_manager
"""

import argparse

from loguru import logger


def main():
    parser = argparse.ArgumentParser(description="Backfill requirements into ICS search queue")
    parser.add_argument("--limit", type=int, default=0, help="Max requirements to enqueue (0=all)")
    parser.add_argument("--dry-run", action="store_true", help="Count only, don't enqueue")
    args = parser.parse_args()

    from app.database import SessionLocal
    from app.models import IcsSearchQueue, Requirement
    from app.services.ics_worker.queue_manager import enqueue_for_ics_search

    db = SessionLocal()
    try:
        # Find requirements with MPNs that don't already have ICS queue entries
        query = (
            db.query(Requirement)
            .filter(
                Requirement.primary_mpn.isnot(None),
                Requirement.primary_mpn != "",
                ~Requirement.id.in_(db.query(IcsSearchQueue.requirement_id)),
            )
            .order_by(Requirement.created_at.desc())
        )

        if args.limit:
            query = query.limit(args.limit)

        requirements = query.all()
        logger.info("Found {} requirements to backfill", len(requirements))

        if args.dry_run:
            logger.info("Dry run — not enqueuing")
            for r in requirements[:10]:
                logger.info("  Would enqueue: requirement {} (mpn={})", r.id, r.primary_mpn)
            if len(requirements) > 10:
                logger.info("  ... and {} more", len(requirements) - 10)
            return

        enqueued = 0
        skipped = 0
        for r in requirements:
            try:
                result = enqueue_for_ics_search(r.id, db)
                if result:
                    enqueued += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.warning("Failed to enqueue requirement {}: {}", r.id, e)
                skipped += 1

        logger.info("Backfill complete: {} enqueued, {} skipped", enqueued, skipped)

    finally:
        db.close()


if __name__ == "__main__":
    main()

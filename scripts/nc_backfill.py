"""Backfill existing requirements into the NC search queue.

Finds requirements with a primary_mpn that are not yet queued for NC search.
Optionally filter by requisition_id or limit the number of items.

Usage:
    PYTHONPATH=/root/availai python scripts/nc_backfill.py [--requisition-id ID] [--limit N] [--dry-run]

Called by: admin / manual one-time operation
Depends on: database, nc_worker.queue_manager
"""

import argparse

from loguru import logger

from app.database import SessionLocal
from app.models import NcSearchQueue, Requirement
from app.services.nc_worker.queue_manager import enqueue_for_nc_search


def main():
    parser = argparse.ArgumentParser(description="Backfill requirements into NC search queue")
    parser.add_argument("--requisition-id", type=int, help="Only backfill requirements from this requisition")
    parser.add_argument("--limit", type=int, default=500, help="Max requirements to enqueue (default 500)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be queued without enqueuing")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # Find requirements with an MPN that are not already in the NC queue
        already_queued = db.query(NcSearchQueue.requirement_id).subquery()
        query = (
            db.query(Requirement)
            .filter(
                Requirement.primary_mpn.isnot(None),
                Requirement.primary_mpn != "",
                ~Requirement.id.in_(db.query(already_queued.c.requirement_id)),
            )
            .order_by(Requirement.id.asc())
        )

        if args.requisition_id:
            query = query.filter(Requirement.requisition_id == args.requisition_id)

        requirements = query.limit(args.limit).all()

        if not requirements:
            logger.info("NC backfill: no unqueued requirements found")
            return

        logger.info("NC backfill: found {} requirements to enqueue", len(requirements))

        enqueued = 0
        skipped = 0
        for req in requirements:
            if args.dry_run:
                logger.info("  [dry-run] would enqueue requirement {} (mpn={})", req.id, req.primary_mpn)
                enqueued += 1
                continue

            result = enqueue_for_nc_search(req.id, db)
            if result:
                enqueued += 1
            else:
                skipped += 1

        logger.info(
            "NC backfill complete: {} enqueued, {} skipped (deduped/no-mpn){}",
            enqueued,
            skipped,
            " [DRY RUN]" if args.dry_run else "",
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()

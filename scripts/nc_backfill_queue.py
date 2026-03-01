"""Backfill nc_search_queue with existing requirements.

Picks one requirement per unique normalized MPN (the most recent),
skipping any MPNs already in the queue. Inserts in bulk with
status='pending' so the AI gate can process them.

Run: python scripts/nc_backfill_queue.py [--dry-run] [--limit N]

Called by: manual one-time script
Depends on: database, nc_worker.mpn_normalizer
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func

from app.database import SessionLocal
from app.models import NcSearchQueue, Requirement
from app.services.nc_worker.mpn_normalizer import normalize_mpn


def backfill(dry_run: bool = False, limit: int = 0):
    db = SessionLocal()
    try:
        # Get MPNs already in the queue (any status)
        existing_mpns = set(row[0] for row in db.query(NcSearchQueue.normalized_mpn).distinct().all() if row[0])
        logger.info("Already in queue: {} unique MPNs", len(existing_mpns))

        # Get all requirements with MPNs, pick the most recent per MPN
        # Use a subquery to get max(id) per primary_mpn
        subq = (
            db.query(
                func.max(Requirement.id).label("id"),
            )
            .filter(Requirement.primary_mpn.isnot(None), Requirement.primary_mpn != "")
            .group_by(Requirement.primary_mpn)
            .subquery()
        )

        requirements = (
            db.query(Requirement).join(subq, Requirement.id == subq.c.id).order_by(Requirement.id.desc()).all()
        )
        logger.info("Unique MPNs in requirements: {}", len(requirements))

        # Build queue entries, skipping already-queued MPNs
        to_insert = []
        skipped = 0
        for req in requirements:
            norm = normalize_mpn(req.primary_mpn)
            if not norm:
                skipped += 1
                continue
            if norm in existing_mpns:
                skipped += 1
                continue

            existing_mpns.add(norm)  # dedup within this batch too
            to_insert.append(
                NcSearchQueue(
                    requirement_id=req.id,
                    requisition_id=req.requisition_id,
                    mpn=req.primary_mpn,
                    normalized_mpn=norm,
                    manufacturer=req.brand,
                    status="pending",
                    priority=5,  # normal priority
                    created_at=datetime.now(timezone.utc),
                )
            )

            if limit and len(to_insert) >= limit:
                break

        logger.info(
            "Backfill: {} to insert, {} skipped (already queued or empty MPN)",
            len(to_insert),
            skipped,
        )

        if dry_run:
            logger.info("DRY RUN — no changes made")
            # Show sample
            for item in to_insert[:10]:
                logger.info("  Would queue: {} (req={})", item.mpn, item.requirement_id)
            if len(to_insert) > 10:
                logger.info("  ... and {} more", len(to_insert) - 10)
            return

        # Bulk insert in batches of 1000
        batch_size = 1000
        inserted = 0
        for i in range(0, len(to_insert), batch_size):
            batch = to_insert[i : i + batch_size]
            db.add_all(batch)
            db.commit()
            inserted += len(batch)
            logger.info("  Inserted batch {}/{}", inserted, len(to_insert))

        logger.info("Backfill complete: {} items queued", inserted)

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill NC search queue")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be queued without inserting")
    parser.add_argument("--limit", type=int, default=0, help="Max items to queue (0 = all)")
    args = parser.parse_args()

    backfill(dry_run=args.dry_run, limit=args.limit)

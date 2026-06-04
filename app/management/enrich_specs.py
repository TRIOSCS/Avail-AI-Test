"""Backfill structured specs for material cards (one-time / on-demand).

Usage: python -m app.management.enrich_specs [--limit N]

Called by: admin manually after deploying the spec-enrichment pipeline.
Depends on: spec_enrichment_service.enrich_pending_specs.
"""

import argparse
import asyncio

from loguru import logger


async def main(limit: int = 100) -> None:
    from app.database import SessionLocal
    from app.services.spec_enrichment_service import enrich_pending_specs

    db = SessionLocal()
    try:
        stats = await enrich_pending_specs(db, limit=limit)
        logger.info("Spec backfill complete: {}", stats)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill structured specs for material cards")
    parser.add_argument("--limit", type=int, default=100, help="Max cards to process")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit))

"""Prefix-lookup tag backfill for material cards (on-demand).

Usage: python -m app.management.prefix_backfill [--batch-size N]

Was the scheduled prefix_backfill job (W1 simplification, 2026-08-04): run
manually when a card import lands and untagged cards need brand/commodity tags.
Called by: admin manually.
Depends on: tagging_backfill.run_prefix_backfill.
"""

import argparse

from loguru import logger


def main(batch_size: int = 1000) -> None:
    from app.database import SessionLocal
    from app.services.tagging_backfill import run_prefix_backfill

    db = SessionLocal()
    try:
        stats = run_prefix_backfill(db, batch_size=batch_size)
        logger.info("Prefix backfill complete: {}", stats)
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prefix-lookup tag backfill for material cards")
    parser.add_argument("--batch-size", type=int, default=1000, help="Cards per batch")
    args = parser.parse_args()
    main(batch_size=args.batch_size)

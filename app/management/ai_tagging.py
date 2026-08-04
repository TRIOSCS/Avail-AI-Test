"""AI (Claude Haiku) brand classification for untagged material cards (on-demand).

Usage: python -m app.management.ai_tagging [--limit N]

Was the scheduled ai_tagging job (W1 simplification, 2026-08-04): paid-API
classification now runs only on-demand, when AI keys are on. Waterfall: run
prefix_backfill first (free), then this for the remainder. Caps at --limit
cards per run (default 500) — rerun to work through the backlog. Internal
parts are always excluded. Thin wrapper: the batching, concurrency, pacing,
and Claude-unconfigured early exit all live in the service.
Called by: admin manually.
Depends on: tagging_ai_batch.run_ai_backfill.
"""

import argparse
import asyncio

from loguru import logger


async def main(limit: int = 500) -> None:
    from app.database import SessionLocal
    from app.services.tagging_ai_batch import run_ai_backfill

    db = SessionLocal()
    try:
        result = await run_ai_backfill(db, limit=limit, exclude_internal=True)
        logger.info("AI tagging done: {}", result)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI brand classification for untagged material cards")
    parser.add_argument("--limit", type=int, default=500, help="Max cards to classify this run")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit))

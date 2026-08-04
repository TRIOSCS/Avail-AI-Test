"""AI (Claude Haiku) brand classification for untagged material cards (on-demand).

Usage: python -m app.management.ai_tagging [--limit N]

Was the scheduled ai_tagging job (W1 simplification, 2026-08-04): paid-API
classification now runs only on-demand, when AI keys are on. Waterfall: run
prefix_backfill first (free), then this for the remainder. Caps at --limit
cards per run (default 500) — rerun to work through the backlog.
Called by: admin manually.
Depends on: tagging_ai.classify_parts_with_ai/_apply_ai_results.
"""

import argparse
import asyncio

from loguru import logger


async def main(limit: int = 500) -> None:
    from app.database import SessionLocal
    from app.models.intelligence import MaterialCard
    from app.models.tags import MaterialTag, Tag
    from app.services.tagging_ai import _apply_ai_results, classify_parts_with_ai

    db = SessionLocal()
    try:
        # Find cards with NO brand tag, excluding internal parts
        tagged_brand_ids = (
            db.query(MaterialTag.material_card_id)
            .join(Tag, MaterialTag.tag_id == Tag.id)
            .filter(Tag.tag_type == "brand")
            .distinct()
            .subquery()
        )
        untagged = (
            db.query(MaterialCard.id, MaterialCard.normalized_mpn)
            .filter(
                ~MaterialCard.id.in_(db.query(tagged_brand_ids.c.material_card_id)),
                MaterialCard.is_internal_part.is_(False),
            )
            .order_by(MaterialCard.id)
            .limit(limit)
            .all()
        )

        if not untagged:
            logger.info("AI tagging: no untagged cards remaining")
            return

        logger.info(f"AI tagging: classifying {len(untagged)} cards")

        total_matched = 0
        total_unknown = 0

        # Process in batches of 50 MPNs, 5 concurrent Claude calls
        batch_size = 50
        concurrency = 5
        sem = asyncio.Semaphore(concurrency)

        async def _classify_batch(batch):
            mpns = [row.normalized_mpn for row in batch]
            async with sem:
                return await classify_parts_with_ai(mpns)

        all_batches = [untagged[i : i + batch_size] for i in range(0, len(untagged), batch_size)]

        for round_start in range(0, len(all_batches), concurrency):
            round_batches = all_batches[round_start : round_start + concurrency]
            results = await asyncio.gather(
                *[_classify_batch(b) for b in round_batches],
                return_exceptions=True,
            )

            for batch, classified in zip(round_batches, results):
                if isinstance(classified, BaseException):
                    logger.warning(f"AI batch failed: {classified}")
                    continue
                batch_tuples = [(row.id, row.normalized_mpn) for row in batch]
                matched, unknown = _apply_ai_results(classified, batch_tuples, db)
                total_matched += matched
                total_unknown += unknown

            db.commit()

        logger.info(f"AI tagging done: {len(untagged)} processed, {total_matched} matched, {total_unknown} unknown")
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

"""Phase 5: Web search enrichment — lifecycle, RoHS, cross-references, pin count.

Uses Claude's web_search tool (real-time, NOT batch) to look up datasheets and
manufacturer product pages for data that cannot be inferred from MPNs alone.

Targets high-value cards first, then expands. Resumable via EnrichmentRun state.

Called by: scripts/enrich_orchestrator.py or manual
Depends on: app.utils.claude_client.claude_json, app.models.intelligence.MaterialCard
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

from loguru import logger

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))

from app.database import SessionLocal
from app.models.enrichment_run import EnrichmentRun
from app.models.intelligence import MaterialCard

MAX_CONCURRENT = 5  # Parallel web search calls
BATCH_COMMIT_SIZE = 50  # Commit every N cards

_SYSTEM = (
    "You are an expert electronic component engineer. Look up the part number on "
    "manufacturer websites and distributor pages to find its lifecycle status, RoHS "
    "compliance, pin count, cross-references (alternative compatible parts), and "
    "datasheet URL. Only report facts you find — do not guess."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "lifecycle_status": {
            "type": ["string", "null"],
            "enum": ["active", "nrfnd", "eol", "obsolete", "ltb", None],
        },
        "rohs_status": {
            "type": ["string", "null"],
            "enum": ["compliant", "non-compliant", "exempt", None],
        },
        "pin_count": {"type": ["integer", "null"]},
        "datasheet_url": {"type": ["string", "null"]},
        "cross_references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mpn": {"type": "string"},
                    "manufacturer": {"type": ["string", "null"]},
                },
                "required": ["mpn"],
            },
        },
        "confidence": {"type": "number"},
    },
    "required": ["confidence"],
}


async def _enrich_single_card(card_id: int, mpn: str, manufacturer: str, semaphore: asyncio.Semaphore) -> dict:
    """Look up a single card via Claude web_search."""
    from app.utils.claude_client import claude_json

    async with semaphore:
        try:
            mfg_str = f" by {manufacturer}" if manufacturer else ""
            prompt = (
                f"Look up the electronic component '{mpn}'{mfg_str}. "
                f"Find: lifecycle status (active/eol/obsolete/nrfnd/ltb), "
                f"RoHS compliance, pin count, datasheet URL, and any cross-reference "
                f"or substitute part numbers."
            )

            result = await claude_json(
                prompt,
                schema=_SCHEMA,
                system=_SYSTEM,
                model_tier="smart",
                max_tokens=2048,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            )

            if result and isinstance(result, dict):
                result["card_id"] = card_id
                return result
            return {"card_id": card_id, "error": "empty_result"}

        except Exception as e:
            logger.warning(f"Web search failed for {mpn}: {e}")
            return {"card_id": card_id, "error": str(e)}


async def run_web_enrichment(db, limit: int = 5000, dry_run: bool = True) -> dict:
    """Run web search enrichment on high-value cards."""
    logger.info("═══ Phase 5: Web search enrichment ═══")

    # Check for resumable run
    existing = (
        db.query(EnrichmentRun)
        .filter(EnrichmentRun.phase == "phase_5_web", EnrichmentRun.status == "running")
        .first()
    )
    already_done = set()
    if existing:
        already_done = set(existing.progress.get("completed_ids", []))
        logger.info(f"Resuming — {len(already_done)} cards already done")
        run = existing
    else:
        run = EnrichmentRun(
            run_id=f"phase_5_web_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}",
            phase="phase_5_web",
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        db.add(run)
        db.commit()

    # Query high-value cards missing lifecycle/RoHS data
    rows = (
        db.query(MaterialCard.id, MaterialCard.display_mpn, MaterialCard.manufacturer)
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.lifecycle_status.is_(None),
            MaterialCard.category.isnot(None),
            MaterialCard.category != "other",
        )
        .order_by(MaterialCard.search_count.desc().nullslast())
        .limit(limit)
        .all()
    )

    # Filter out already-done
    cards = [(r.id, r.display_mpn, r.manufacturer) for r in rows if r.id not in already_done]
    logger.info(f"  Cards to process: {len(cards)}")

    if not cards:
        run.status = "completed"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        return {"total": 0}

    stats = {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}
    completed_ids = list(already_done)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    # Process in batches of BATCH_COMMIT_SIZE
    for batch_start in range(0, len(cards), BATCH_COMMIT_SIZE):
        batch = cards[batch_start: batch_start + BATCH_COMMIT_SIZE]

        if dry_run:
            stats["processed"] += len(batch)
            stats["skipped"] += len(batch)
            continue

        tasks = [
            _enrich_single_card(cid, mpn, mfg, semaphore)
            for cid, mpn, mfg in batch
        ]
        results = await asyncio.gather(*tasks)

        for result in results:
            card_id = result.get("card_id")
            stats["processed"] += 1

            if "error" in result:
                stats["errors"] += 1
                continue

            conf = result.get("confidence", 0.0)
            if conf < 0.70:
                stats["skipped"] += 1
                continue

            updates = {}
            if result.get("lifecycle_status"):
                updates["lifecycle_status"] = result["lifecycle_status"]
            if result.get("rohs_status"):
                updates["rohs_status"] = result["rohs_status"]
            if result.get("pin_count"):
                updates["pin_count"] = result["pin_count"]
            if result.get("datasheet_url") and isinstance(result["datasheet_url"], str):
                updates["datasheet_url"] = result["datasheet_url"][:1000]
            if result.get("cross_references"):
                updates["cross_references"] = result["cross_references"]

            if updates:
                db.query(MaterialCard).filter(MaterialCard.id == card_id).update(
                    updates, synchronize_session=False,
                )
                stats["updated"] += 1
                completed_ids.append(card_id)
            else:
                stats["skipped"] += 1

        db.commit()

        # Save checkpoint
        run.progress = {**stats, "completed_ids": completed_ids[-1000:]}  # Keep last 1000 for resume
        db.commit()

        if stats["processed"] % 500 == 0:
            logger.info(f"  Phase 5 progress: {stats}")

    run.status = "completed"
    run.stats = stats
    run.completed_at = datetime.now(timezone.utc)
    db.commit()

    mode = "DRY RUN" if dry_run else "APPLIED"
    logger.info(f"[{mode}] Phase 5 complete: {stats}")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 5: Web search enrichment")
    parser.add_argument("--apply", action="store_true", help="Actually write (default: dry run)")
    parser.add_argument("--limit", type=int, default=5000, help="Max cards (default: 5000)")
    args = parser.parse_args()

    async def main():
        db = SessionLocal()
        result = await run_web_enrichment(db, limit=args.limit, dry_run=not args.apply)
        logger.info(result)
        db.close()

    asyncio.run(main())

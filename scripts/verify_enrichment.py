"""Phase 6: Cross-verification — 1000-card stratified sample verified by Sonnet + web search.

Validates enrichment accuracy before making data visible to users.
Generates an accuracy report with per-category and per-field metrics.

Called by: scripts/enrich_orchestrator.py or manual
Depends on: app.utils.claude_client.claude_json, app.models.intelligence.MaterialCard
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

from loguru import logger

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))

from app.database import SessionLocal
from app.models.intelligence import MaterialCard
from sqlalchemy import func

SAMPLE_SIZE = 1000
MAX_CONCURRENT = 5

_SYSTEM = (
    "You are verifying the accuracy of an electronic component database. "
    "For each part, search the web to check if the stored category, description, "
    "and specs are correct. Rate each field as 'correct', 'incorrect', or 'uncertain'."
)


async def _verify_card(card: dict, semaphore: asyncio.Semaphore) -> dict:
    """Verify a single card's enrichment data via web search."""
    from app.utils.claude_client import claude_json

    async with semaphore:
        try:
            prompt = (
                f"Verify this electronic component record:\n\n"
                f"MPN: {card['display_mpn']}\n"
                f"Manufacturer: {card.get('manufacturer', 'unknown')}\n"
                f"Category: {card.get('category', 'unknown')}\n"
                f"Description: {card.get('description', 'none')}\n"
                f"Specs: {card.get('specs_summary', 'none')}\n\n"
                f"Search the web to verify. For each field, rate as 'correct', 'incorrect', or 'uncertain'."
            )

            result = await claude_json(
                prompt,
                schema={
                    "type": "object",
                    "properties": {
                        "category_verdict": {"type": "string", "enum": ["correct", "incorrect", "uncertain"]},
                        "description_verdict": {"type": "string", "enum": ["correct", "incorrect", "uncertain"]},
                        "manufacturer_verdict": {"type": "string", "enum": ["correct", "incorrect", "uncertain"]},
                        "specs_verdict": {"type": "string", "enum": ["correct", "incorrect", "uncertain", "not_applicable"]},
                        "notes": {"type": "string"},
                    },
                    "required": ["category_verdict", "description_verdict"],
                },
                system=_SYSTEM,
                model_tier="smart",
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            )

            if result:
                result["card_id"] = card["id"]
                result["category"] = card.get("category")
                return result

        except Exception as e:
            logger.warning(f"Verification failed for {card['display_mpn']}: {e}")

        return {"card_id": card["id"], "category": card.get("category"), "error": True}


async def run_verification(db, sample_size: int = SAMPLE_SIZE) -> dict:
    """Run stratified verification on a sample of enriched cards."""
    logger.info(f"═══ Phase 6: Cross-verification ({sample_size} cards) ═══")

    # Get category distribution for stratified sampling
    cat_counts = (
        db.query(MaterialCard.category, func.count(MaterialCard.id))
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.category.isnot(None),
            MaterialCard.category != "other",
            MaterialCard.enrichment_source.isnot(None),
        )
        .group_by(MaterialCard.category)
        .all()
    )
    total = sum(c for _, c in cat_counts)
    if total == 0:
        return {"error": "No enriched cards to verify"}

    # Stratified sampling: proportional to category size, min 5 per category
    sample_cards = []
    for cat, count in cat_counts:
        n = max(5, int(sample_size * count / total))
        cards = (
            db.query(
                MaterialCard.id, MaterialCard.display_mpn,
                MaterialCard.manufacturer, MaterialCard.category,
                MaterialCard.description, MaterialCard.specs_summary,
            )
            .filter(
                MaterialCard.deleted_at.is_(None),
                MaterialCard.category == cat,
                MaterialCard.enrichment_source.isnot(None),
            )
            .order_by(func.random())
            .limit(n)
            .all()
        )
        for c in cards:
            sample_cards.append({
                "id": c.id,
                "display_mpn": c.display_mpn,
                "manufacturer": c.manufacturer,
                "category": c.category,
                "description": c.description,
                "specs_summary": c.specs_summary,
            })

    logger.info(f"  Sampled {len(sample_cards)} cards across {len(cat_counts)} categories")

    # Run verification
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = [_verify_card(card, semaphore) for card in sample_cards]
    results = await asyncio.gather(*tasks)

    # Compute accuracy metrics
    field_verdicts = defaultdict(lambda: {"correct": 0, "incorrect": 0, "uncertain": 0, "error": 0})
    cat_verdicts = defaultdict(lambda: {"correct": 0, "incorrect": 0, "uncertain": 0})

    for r in results:
        if r.get("error"):
            field_verdicts["category"]["error"] += 1
            continue

        cat = r.get("category", "unknown")

        for field in ["category", "description", "manufacturer"]:
            verdict = r.get(f"{field}_verdict", "uncertain")
            field_verdicts[field][verdict] += 1
            if field == "category":
                cat_verdicts[cat][verdict] += 1

        specs_v = r.get("specs_verdict", "not_applicable")
        if specs_v != "not_applicable":
            field_verdicts["specs"][specs_v] += 1

    # Calculate accuracy percentages
    report = {"sample_size": len(sample_cards), "field_accuracy": {}, "category_accuracy": {}}

    for field, counts in field_verdicts.items():
        total_rated = counts["correct"] + counts["incorrect"]
        accuracy = (counts["correct"] / total_rated * 100) if total_rated > 0 else 0
        report["field_accuracy"][field] = {
            "accuracy_pct": round(accuracy, 1),
            "correct": counts["correct"],
            "incorrect": counts["incorrect"],
            "uncertain": counts["uncertain"],
            "errors": counts.get("error", 0),
        }

    for cat, counts in cat_verdicts.items():
        total_rated = counts["correct"] + counts["incorrect"]
        accuracy = (counts["correct"] / total_rated * 100) if total_rated > 0 else 0
        report["category_accuracy"][cat] = {
            "accuracy_pct": round(accuracy, 1),
            "sample_n": sum(counts.values()),
        }

    # Targets
    targets_met = {
        "category": report["field_accuracy"].get("category", {}).get("accuracy_pct", 0) >= 95,
        "description": report["field_accuracy"].get("description", {}).get("accuracy_pct", 0) >= 95,
        "specs": report["field_accuracy"].get("specs", {}).get("accuracy_pct", 0) >= 90,
    }
    report["targets_met"] = targets_met
    report["all_targets_met"] = all(targets_met.values())

    # Save report
    os.makedirs("docs/superpowers/reports", exist_ok=True)
    report_path = f"docs/superpowers/reports/enrichment_verification_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"Verification report saved: {report_path}")
    logger.info(f"Field accuracy: {json.dumps(report['field_accuracy'], indent=2)}")
    logger.info(f"Targets met: {report['targets_met']}")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 6: Enrichment verification")
    parser.add_argument("--sample-size", type=int, default=SAMPLE_SIZE, help="Cards to verify (default: 1000)")
    args = parser.parse_args()

    async def main():
        db = SessionLocal()
        report = await run_verification(db, sample_size=args.sample_size)
        db.close()

    asyncio.run(main())

"""Batch AI category correction for MaterialCards.

Uses Claude Haiku to classify cards into standardized commodity categories.
Called by: manual one-time script
Depends on: app.config.settings, app.models.intelligence.MaterialCard, anthropic
"""

# Bootstrap app imports — works both locally and in Docker
import os
import sys
import time

import anthropic
from loguru import logger
from sqlalchemy import func, or_

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))
from app.config import settings
from app.database import SessionLocal
from app.models.intelligence import MaterialCard
from app.services.specialty_detector import COMMODITY_MAP

# Use the canonical 45-category taxonomy from specialty_detector (single source of truth).
VALID_CATEGORIES = sorted(COMMODITY_MAP.keys())

BATCH_SIZE = 50
MAX_BATCHES = 2000  # Safety cap


def classify_batch(client: anthropic.Anthropic, cards: list[dict]) -> dict:
    """Send a batch of cards to Haiku for classification.

    Returns {id: category}.
    """
    lines = []
    for c in cards:
        desc = (c["description"] or "")[:120]
        mfg = c["manufacturer"] or ""
        lines.append(f"{c['id']}|{c['mpn']}|{mfg}|{desc}")

    items_text = "\n".join(lines)
    cats_text = ", ".join(VALID_CATEGORIES)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": f"""Classify each electronic component into exactly one category.

Categories: {cats_text}

Format: one line per item, just "id|category". No explanations.

Items (id|mpn|manufacturer|description):
{items_text}""",
            }
        ],
    )

    results = {}
    for line in response.content[0].text.strip().split("\n"):
        line = line.strip()
        if "|" not in line:
            continue
        parts = line.split("|", 1)
        try:
            card_id = int(parts[0].strip())
            cat = parts[1].strip().lower().replace(" ", "_")
            if cat in VALID_CATEGORIES:
                results[card_id] = cat
        except (ValueError, IndexError):
            continue

    return results


def main():
    db = SessionLocal()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    # Coarse categories that need reclassification into granular ones
    RECLASSIFY = {"servers", "storage", "memory", "processors", "Microprocessors"}

    # Cards that need fixing: bad/coarse categories + uncategorized
    cards_to_fix = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn, MaterialCard.description, MaterialCard.manufacturer)
        .filter(
            MaterialCard.deleted_at.is_(None),
            or_(
                # Uncategorized
                MaterialCard.category.is_(None),
                MaterialCard.category == "",
                # "other" bucket
                MaterialCard.category == "other",
                # Coarse categories that need granular reclassification
                MaterialCard.category.in_(RECLASSIFY),
                # Long description used as category (junk)
                func.length(MaterialCard.category) > 25,
            ),
            # Must have a description to classify meaningfully
            MaterialCard.description.isnot(None),
            MaterialCard.description != "",
        )
        .order_by(MaterialCard.search_count.desc().nullslast())
        .limit(BATCH_SIZE * MAX_BATCHES)
        .all()
    )

    total = len(cards_to_fix)
    logger.info(f"Cards to classify: {total}")

    if total == 0:
        logger.info("Nothing to do.")
        return

    updated = 0
    errors = 0
    batch_num = 0

    for i in range(0, total, BATCH_SIZE):
        batch = cards_to_fix[i : i + BATCH_SIZE]
        batch_dicts = [
            {"id": c.id, "mpn": c.normalized_mpn, "description": c.description, "manufacturer": c.manufacturer}
            for c in batch
        ]
        batch_num += 1

        try:
            results = classify_batch(client, batch_dicts)

            for card_id, cat in results.items():
                if cat != "other":  # Only update if we got a real category
                    db.query(MaterialCard).filter(MaterialCard.id == card_id).update({"category": cat})
                    updated += 1

            db.commit()

            if batch_num % 10 == 0:
                logger.info(
                    f"Batch {batch_num}/{(total + BATCH_SIZE - 1) // BATCH_SIZE} — {updated} updated, {errors} errors"
                )

        except anthropic.RateLimitError:
            logger.warning("Rate limited, waiting 30s...")
            time.sleep(30)
            # Retry this batch
            try:
                results = classify_batch(client, batch_dicts)
                for card_id, cat in results.items():
                    if cat != "other":
                        db.query(MaterialCard).filter(MaterialCard.id == card_id).update({"category": cat})
                        updated += 1
                db.commit()
            except Exception as e:
                logger.error(f"Retry failed: {e}")
                errors += len(batch)

        except Exception as e:
            logger.error(f"Batch {batch_num} failed: {e}")
            errors += len(batch)
            db.rollback()

        # Small delay to avoid rate limits
        time.sleep(0.3)

    logger.info(f"Done. Updated: {updated}, Errors: {errors}, Total processed: {total}")
    db.close()


if __name__ == "__main__":
    main()

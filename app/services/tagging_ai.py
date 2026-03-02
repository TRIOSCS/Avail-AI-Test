"""AI fallback classification — classify remaining untagged parts via Claude Haiku.

Last resort in the classification waterfall. Batches 20-50 MPNs per API call,
parses structured JSON response, creates MaterialTags with source='ai_classified'.

Also provides chunked batch result applier for large Anthropic Batch API results.

Called by: app.routers.tagging_admin (manual trigger)
Depends on: app.utils.claude_client, app.services.tagging
"""

import asyncio
import json
import tempfile

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging import (
    get_or_create_brand_tag,
    get_or_create_commodity_tag,
    tag_material_card,
)

_CLASSIFY_PROMPT = """Classify these electronic component part numbers. For each MPN, provide:
- manufacturer: The most likely manufacturer (full company name)
- category: The component category (e.g., Microcontrollers (MCU), Capacitors, Connectors, etc.)

Return a JSON array with one object per part:
[{{"mpn": "...", "manufacturer": "...", "category": "..."}}]

If you're unsure about a part, use "Unknown" for manufacturer and "Miscellaneous" for category.

Part numbers to classify:
{mpns}"""

_SYSTEM = "You are an expert electronic component classifier. Return only valid JSON."


async def classify_parts_with_ai(part_numbers: list[str]) -> list[dict]:  # pragma: no cover
    """Batch MPNs per Claude Haiku call. Parse structured JSON response."""
    from app.utils.claude_client import claude_json

    mpn_list = "\n".join(f"- {mpn}" for mpn in part_numbers)
    prompt = _CLASSIFY_PROMPT.format(mpns=mpn_list)

    result = await claude_json(
        prompt,
        system=_SYSTEM,
        model_tier="fast",
        max_tokens=4096,
        timeout=60,
    )

    if not result or not isinstance(result, list):
        logger.warning("AI classification returned invalid response")
        return [{"mpn": mpn, "manufacturer": "Unknown", "category": "Miscellaneous"} for mpn in part_numbers]

    # Validate and normalize response
    classified = []
    for item in result:
        if not isinstance(item, dict):  # pragma: no cover
            continue
        classified.append({
            "mpn": (item.get("mpn") or "").strip(),
            "manufacturer": (item.get("manufacturer") or "Unknown").strip(),
            "category": (item.get("category") or "Miscellaneous").strip(),
        })

    return classified


def _apply_ai_results(classified: list[dict], batch: list, db: Session) -> tuple[int, int]:  # pragma: no cover
    """Apply classification results to DB. Returns (matched, unknown) counts."""
    matched = 0
    unknown = 0
    mpn_to_result = {c["mpn"].lower(): c for c in classified}

    for card_id, normalized_mpn in batch:
        result = mpn_to_result.get(normalized_mpn.lower(), {})
        manufacturer = result.get("manufacturer", "Unknown")
        category = result.get("category", "Miscellaneous")

        is_unknown = manufacturer == "Unknown"
        confidence = 0.3 if is_unknown else 0.7

        tags_to_apply = []

        brand_tag = get_or_create_brand_tag(manufacturer, db)
        tags_to_apply.append({
            "tag_id": brand_tag.id,
            "source": "ai_classified",
            "confidence": confidence,
        })

        commodity_tag = get_or_create_commodity_tag(category, db)
        if commodity_tag:  # pragma: no cover
            tags_to_apply.append({
                "tag_id": commodity_tag.id,
                "source": "ai_classified",
                "confidence": confidence,
            })

        tag_material_card(card_id, tags_to_apply, db)

        if is_unknown:
            unknown += 1
        else:
            matched += 1

        if not is_unknown:
            card = db.get(MaterialCard, card_id)
            if card and not card.manufacturer:
                card.manufacturer = manufacturer

    return matched, unknown


async def run_ai_backfill(db: Session, batch_size: int = 50, concurrency: int = 10) -> dict:  # pragma: no cover
    """Classify remaining untagged parts after Nexar. Concurrent API calls.

    Args:
        batch_size: MPNs per Claude API call (default 50)
        concurrency: Parallel API calls (default 10, so 500 MPNs per round)

    Returns: {total_processed, total_matched, total_unknown}
    """
    # Find cards with NO brand tag
    tagged_brand_ids = (
        db.query(MaterialTag.material_card_id)
        .join(Tag, MaterialTag.tag_id == Tag.id)
        .filter(Tag.tag_type == "brand")
        .distinct()
        .subquery()
    )
    untagged = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn)
        .filter(~MaterialCard.id.in_(db.query(tagged_brand_ids.c.material_card_id)))
        .order_by(MaterialCard.id)
        .all()
    )

    if not untagged:
        logger.info("No untagged cards for AI backfill")
        return {"total_processed": 0, "total_matched": 0, "total_unknown": 0}

    logger.info(f"AI backfill starting: {len(untagged)} untagged cards (batch={batch_size}, concurrency={concurrency})")
    total_processed = 0
    total_matched = 0
    total_unknown = 0

    # Semaphore limits concurrent API calls; delay paces token usage
    sem = asyncio.Semaphore(concurrency)

    async def _classify_batch(batch):
        mpns = [row.normalized_mpn for row in batch]
        async with sem:
            for attempt in range(3):
                try:
                    return await classify_parts_with_ai(mpns)
                except Exception as e:
                    if "rate_limit" in str(e).lower() or "429" in str(e):
                        wait = 10 * (attempt + 1)
                        logger.warning(f"Rate limited, waiting {wait}s (attempt {attempt + 1}/3)")
                        await asyncio.sleep(wait)
                    else:
                        logger.exception("AI classification batch failed")
                        break
            return [{"mpn": mpn, "manufacturer": "Unknown", "category": "Miscellaneous"} for mpn in mpns]

    # Split all untagged into sub-batches
    all_batches = [
        untagged[j : j + batch_size]
        for j in range(0, len(untagged), batch_size)
    ]

    # Process in rounds of (concurrency) batches with a delay between rounds
    for round_start in range(0, len(all_batches), concurrency):
        round_batches = all_batches[round_start : round_start + concurrency]

        results = await asyncio.gather(*[_classify_batch(b) for b in round_batches])

        for batch, classified in zip(round_batches, results):
            matched, unknown = _apply_ai_results(classified, batch, db)
            total_matched += matched
            total_unknown += unknown
            total_processed += len(batch)

        db.commit()
        logger.info(f"AI backfill: {total_processed}/{len(untagged)} — {total_matched} matched, {total_unknown} unknown")

        # Pace to stay under 90K output tokens/min (~3K tokens per batch)
        # concurrency batches * 3K tokens = tokens_per_round
        # Target: stay under 80K/min to leave headroom
        if round_start + concurrency < len(all_batches):
            tokens_per_round = concurrency * 3000
            delay = max(1.0, (tokens_per_round / 80000) * 60)
            await asyncio.sleep(delay)

    logger.info(f"AI backfill complete: {total_processed} processed, {total_matched} matched, {total_unknown} unknown")
    return {"total_processed": total_processed, "total_matched": total_matched, "total_unknown": total_unknown}


async def apply_batch_results_chunked(batch_id: str) -> dict:
    """Download and apply Batch API results without loading all into memory.

    Streams the results JSONL to a temp file, then processes line by line
    in batches of 100. Safe for 500K+ results in a 2GB container.

    Args:
        batch_id: Anthropic Batch API batch ID

    Returns: {total_lines, matched, unknown, errors}
    """
    from app.database import SessionLocal
    from app.http_client import http
    from app.services.credential_service import get_credential_cached

    api_key = get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "No Anthropic API key configured"}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    # Step 1: Check batch status and get results_url
    resp = await http.get(
        f"https://api.anthropic.com/v1/messages/batches/{batch_id}",
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        return {"error": f"Batch status check failed: HTTP {resp.status_code}"}

    data = resp.json()
    if data.get("processing_status") != "ended":
        return {"error": f"Batch not ready: status={data.get('processing_status')}"}

    results_url = data.get("results_url")
    if not results_url:
        return {"error": "Batch ended but no results_url"}

    # Step 2: Stream results to temp file (avoid loading into memory)
    logger.info(f"Downloading batch {batch_id} results to temp file...")
    tmp_path = tempfile.mktemp(suffix=".jsonl", dir="/tmp")
    try:
        async with http.stream("GET", results_url, headers=headers, timeout=300) as stream:
            with open(tmp_path, "wb") as f:
                async for chunk in stream.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
    except Exception as e:
        return {"error": f"Download failed: {e}"}

    logger.info(f"Batch results downloaded to {tmp_path}")

    # Step 3: Process line by line in batches of 100
    db = SessionLocal()
    total_lines = 0
    matched = 0
    unknown = 0
    errors = 0

    try:
        with open(tmp_path) as f:
            batch_classifications = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total_lines += 1

                try:
                    entry = json.loads(line)
                    result = entry.get("result", {})

                    if result.get("type") != "succeeded":
                        errors += 1
                        continue

                    # Extract tool_use input (classifications array)
                    message = result.get("message", {})
                    classifications = None
                    for block in message.get("content", []):
                        if block.get("type") == "tool_use" and block.get("name") == "structured_output":
                            classifications = block.get("input")
                            break

                    if not classifications:
                        errors += 1
                        continue

                    # classifications is a dict with a 'classifications' array
                    items = classifications.get("classifications", [])
                    if isinstance(classifications, list):
                        items = classifications
                    batch_classifications.extend(items)

                except (json.JSONDecodeError, KeyError):
                    errors += 1
                    continue

                # Process in batches of 100
                if len(batch_classifications) >= 100:
                    m, u = _apply_chunked_batch(batch_classifications, db)
                    matched += m
                    unknown += u
                    batch_classifications = []
                    db.expire_all()

                if total_lines % 500 == 0:
                    logger.info(
                        f"Batch apply progress: {total_lines} lines, "
                        f"{matched} matched, {unknown} unknown, {errors} errors"
                    )

            # Process remaining
            if batch_classifications:
                m, u = _apply_chunked_batch(batch_classifications, db)
                matched += m
                unknown += u

        db.commit()
    except Exception:
        logger.exception("Batch apply failed")
        db.rollback()
        raise
    finally:
        db.close()
        # Clean up temp file
        import os
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    logger.info(
        f"Batch {batch_id} applied: {total_lines} lines, "
        f"{matched} matched, {unknown} unknown, {errors} errors"
    )
    return {"total_lines": total_lines, "matched": matched, "unknown": unknown, "errors": errors}


def _apply_chunked_batch(classifications: list[dict], db: Session) -> tuple[int, int]:
    """Apply a small batch of classifications to the DB. Returns (matched, unknown)."""
    matched = 0
    unknown = 0

    # Collect MPNs and look up cards in one query
    mpn_map = {}
    for item in classifications:
        mpn = (item.get("mpn") or "").strip().lower()
        if mpn:
            mpn_map[mpn] = item

    if not mpn_map:
        return 0, 0

    # Fetch cards in a single query
    cards = (
        db.query(MaterialCard)
        .filter(MaterialCard.normalized_mpn.in_(list(mpn_map.keys())))
        .all()
    )

    for card in cards:
        item = mpn_map.get(card.normalized_mpn.lower(), {})
        manufacturer = (item.get("manufacturer") or "Unknown").strip()
        category = (item.get("category") or "Miscellaneous").strip()

        is_unknown = manufacturer == "Unknown"
        confidence = 0.3 if is_unknown else 0.7

        tags_to_apply = []
        brand_tag = get_or_create_brand_tag(manufacturer, db)
        tags_to_apply.append({
            "tag_id": brand_tag.id,
            "source": "ai_classified",
            "confidence": confidence,
        })

        commodity_tag = get_or_create_commodity_tag(category, db)
        if commodity_tag:
            tags_to_apply.append({
                "tag_id": commodity_tag.id,
                "source": "ai_classified",
                "confidence": confidence,
            })

        tag_material_card(card.id, tags_to_apply, db)

        if not is_unknown and not card.manufacturer:
            card.manufacturer = manufacturer

        if is_unknown:
            unknown += 1
        else:
            matched += 1

    db.commit()
    return matched, unknown

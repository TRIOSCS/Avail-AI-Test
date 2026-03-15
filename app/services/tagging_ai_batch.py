"""Batch AI classification — Anthropic Batch API submit/apply/check.

Handles large-scale MPN classification via the Batch API (50% cheaper, no rate limits)
and concurrent real-time backfill as fallback.

Called by: app.routers.tagging_admin
Depends on: tagging_ai_classify, app.utils.claude_client, app.http_client
"""

import asyncio
import json
import tempfile
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.tags import MaterialTag, Tag
from app.services.tagging import (
    get_or_create_brand_tag,
    get_or_create_commodity_tag,
    tag_material_card,
)
from app.services.tagging_ai_classify import (
    _CLASSIFY_PROMPT,
    _SYSTEM,
    _apply_ai_results,
    classify_parts_with_ai,
)

_BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mpn": {"type": "string"},
                    "manufacturer": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["mpn", "manufacturer", "category"],
            },
        }
    },
    "required": ["classifications"],
}


async def submit_batch_backfill(db: Session, batch_size: int = 100) -> dict:  # pragma: no cover
    """Submit all untagged cards to Anthropic Batch API. 50% cheaper, no rate limits.

    Returns: {batch_id, total_requests, total_mpns} or {error: str}
    """
    from app.utils.claude_client import claude_batch_submit

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
        logger.info("No untagged cards for batch AI backfill")
        return {"batch_id": None, "total_requests": 0, "total_mpns": 0}

    logger.info(f"Batch AI backfill: preparing {len(untagged)} cards in batches of {batch_size}")

    # Build batch requests — each request classifies batch_size MPNs
    requests = []
    for i in range(0, len(untagged), batch_size):
        batch = untagged[i : i + batch_size]
        mpns = [row.normalized_mpn for row in batch]
        mpn_list = "\n".join(f"- {mpn}" for mpn in mpns)
        prompt = _CLASSIFY_PROMPT.format(mpns=mpn_list)

        custom_id = f"batch_{i // batch_size}"

        requests.append(
            {
                "custom_id": custom_id,
                "prompt": prompt,
                "schema": _BATCH_SCHEMA,
                "system": _SYSTEM,
                "model_tier": "fast",
                "max_tokens": 4096,
            }
        )

    # Store batch metadata for result processing
    # Save card_id→mpn mapping to a temp table or file
    batch_meta = {}
    for i in range(0, len(untagged), batch_size):
        batch = untagged[i : i + batch_size]
        batch_key = f"batch_{i // batch_size}"
        batch_meta[batch_key] = [(row.id, row.normalized_mpn) for row in batch]

    logger.info(f"Submitting {len(requests)} batch requests ({len(untagged)} MPNs)")

    # Batch API limit is 100K requests — split if needed
    batch_ids = []
    chunk_size = 50000  # Stay well under 100K limit
    for chunk_start in range(0, len(requests), chunk_size):
        chunk = requests[chunk_start : chunk_start + chunk_size]
        batch_id = await claude_batch_submit(chunk)
        if not batch_id:
            return {"error": f"Batch submit failed at chunk {chunk_start}"}
        batch_ids.append(batch_id)
        logger.info(f"Submitted batch chunk: {batch_id} ({len(chunk)} requests)")

    # Persist metadata so we can process results later.
    # Use per-batch file names to avoid cross-run clobbering.
    meta_path = f"/tmp/ai_backfill_meta_{batch_ids[0] if batch_ids else 'none'}.json"
    with open(meta_path, "w") as f:
        json.dump(
            {
                "batch_ids": batch_ids,
                "batch_meta": {k: v for k, v in batch_meta.items()},
                "total_mpns": len(untagged),
            },
            f,
        )

    logger.info(f"Batch metadata saved to {meta_path}")

    return {
        "batch_ids": batch_ids,
        "total_requests": len(requests),
        "total_mpns": len(untagged),
        "meta_path": meta_path,
    }


async def check_and_apply_batch_results(db: Session, meta_path: str | None = None) -> dict:  # pragma: no cover
    """Poll batch status and apply results if complete.

    Returns: {status, ...} where status is 'processing', 'complete', or 'error'
    """
    from app.utils.claude_client import claude_batch_results

    if not meta_path:
        candidates = sorted(Path("/tmp").glob("ai_backfill_meta_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        meta_path = str(candidates[0]) if candidates else "/tmp/ai_backfill_meta.json"
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except FileNotFoundError:
        return {"status": "error", "error": "No batch metadata found. Run submit_batch_backfill first."}

    batch_ids = meta["batch_ids"]
    batch_meta = meta["batch_meta"]
    total_mpns = meta["total_mpns"]

    total_matched = 0
    total_unknown = 0
    total_processed = 0
    all_complete = True

    for batch_id in batch_ids:
        results = await claude_batch_results(batch_id)
        if results is None:
            all_complete = False
            logger.info(f"Batch {batch_id} still processing")
            continue

        # Apply results
        for custom_id, parsed in results.items():
            if custom_id not in batch_meta:
                continue

            batch = batch_meta[custom_id]

            if parsed and isinstance(parsed, dict):
                classifications = parsed.get("classifications", [])
                # Normalize to list[dict] format
                classified = []
                for item in classifications:
                    if isinstance(item, dict):
                        classified.append(
                            {
                                "mpn": (item.get("mpn") or "").strip(),
                                "manufacturer": (item.get("manufacturer") or "Unknown").strip(),
                                "category": (item.get("category") or "Miscellaneous").strip(),
                            }
                        )
            else:
                # Failed request — mark as unknown
                classified = [{"mpn": mpn, "manufacturer": "Unknown", "category": "Miscellaneous"} for _, mpn in batch]

            matched, unknown = _apply_ai_results(classified, batch, db)
            total_matched += matched
            total_unknown += unknown
            total_processed += len(batch)

            if total_processed % 10000 == 0:
                db.commit()
                logger.info(f"Batch results: {total_processed}/{total_mpns} applied — {total_matched} matched")

        db.commit()

    if all_complete:
        logger.info(
            f"Batch AI backfill complete: {total_processed} processed, {total_matched} matched, {total_unknown} unknown"
        )
        # Clean up metadata
        import os

        os.remove(meta_path)
        return {
            "status": "complete",
            "total_processed": total_processed,
            "total_matched": total_matched,
            "total_unknown": total_unknown,
        }

    return {
        "status": "processing",
        "total_processed": total_processed,
        "total_matched": total_matched,
        "total_unknown": total_unknown,
        "message": "Some batches still processing. Run again later.",
    }


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
    all_batches = [untagged[j : j + batch_size] for j in range(0, len(untagged), batch_size)]

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
        logger.info(
            f"AI backfill: {total_processed}/{len(untagged)} — {total_matched} matched, {total_unknown} unknown"
        )

        # Pace to stay under 90K output tokens/min (~3K tokens per batch)
        # concurrency batches * 3K tokens = tokens_per_round
        # Target: stay under 80K/min to leave headroom
        if round_start + concurrency < len(all_batches):
            tokens_per_round = concurrency * 3000
            delay = max(1.0, (tokens_per_round / 80000) * 60)
            await asyncio.sleep(delay)

    logger.info(f"AI backfill complete: {total_processed} processed, {total_matched} matched, {total_unknown} unknown")
    return {"total_processed": total_processed, "total_matched": total_matched, "total_unknown": total_unknown}


async def submit_targeted_backfill(db: Session, limit: int = 50000) -> dict:
    """Submit untagged cards (no MaterialTag at all) to Anthropic Batch API.

    Excludes cards that already failed AI classification (0.30 "Unknown" tags).
    Groups into batches of 100 MPNs each, submits as a single Batch API job.

    Returns: {batch_id, total_submitted} or {error: str}
    """
    from app.http_client import http
    from app.services.credential_service import get_credential_cached

    api_key = get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
    if not api_key:
        return {"error": "No Anthropic API key configured"}

    # Cards with NO MaterialTag at all AND no manufacturer field
    tagged_ids = db.query(MaterialTag.material_card_id).distinct().subquery()
    untagged = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn)
        .filter(
            ~MaterialCard.id.in_(db.query(tagged_ids.c.material_card_id)),
            (MaterialCard.manufacturer.is_(None) | (MaterialCard.manufacturer == "")),
        )
        .order_by(MaterialCard.id)
        .limit(limit)
        .all()
    )

    if not untagged:
        return {"batch_id": None, "total_submitted": 0}

    logger.info(f"Targeted backfill: preparing {len(untagged)} cards for Batch API")

    # Build batch requests — 100 MPNs per request
    requests = []
    for i in range(0, len(untagged), 100):
        batch = untagged[i : i + 100]
        mpn_list = "\n".join(f"- {row.normalized_mpn}" for row in batch)
        prompt = _CLASSIFY_PROMPT.format(mpns=mpn_list)

        requests.append(
            {
                "custom_id": f"backfill_{i}",
                "params": {
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 8192,
                    "system": _SYSTEM,
                    "messages": [{"role": "user", "content": prompt}],
                },
            }
        )

    # Submit to Batch API
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    # Write requests as JSONL
    resp = await http.post(
        "https://api.anthropic.com/v1/messages/batches",
        headers=headers,
        json={"requests": requests},
        timeout=60,
    )

    if resp.status_code not in (200, 201):
        return {"error": f"Batch API submission failed: HTTP {resp.status_code} — {resp.text[:200]}"}

    data = resp.json()
    batch_id = data.get("id", "unknown")
    logger.info(f"Targeted backfill submitted: batch_id={batch_id}, {len(untagged)} cards in {len(requests)} requests")

    return {"batch_id": batch_id, "total_submitted": len(untagged)}


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
    tmp_file = tempfile.NamedTemporaryFile(suffix=".jsonl", dir="/tmp", delete=False)
    tmp_path = tmp_file.name
    tmp_file.close()
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

    logger.info(f"Batch {batch_id} applied: {total_lines} lines, {matched} matched, {unknown} unknown, {errors} errors")
    return {"total_lines": total_lines, "matched": matched, "unknown": unknown, "errors": errors}


def _apply_chunked_batch(classifications: list[dict], db: Session) -> tuple[int, int]:
    """Apply a small batch of classifications to the DB.

    Returns (matched, unknown).
    """
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
    cards = db.query(MaterialCard).filter(MaterialCard.normalized_mpn.in_(list(mpn_map.keys()))).all()

    for card in cards:
        item = mpn_map.get(card.normalized_mpn.lower(), {})
        manufacturer = (item.get("manufacturer") or "").strip() or None
        category = (item.get("category") or "").strip() or None
        model_confidence = item.get("confidence")

        # v2 schema: skip null/Unknown manufacturers entirely (don't create 0.30 junk tags)
        if not manufacturer or manufacturer == "Unknown":
            unknown += 1
            continue

        # Use model-reported confidence if available and >= 0.90, otherwise default 0.92
        if model_confidence is not None and isinstance(model_confidence, (int, float)):
            confidence = max(0.90, min(1.0, float(model_confidence)))
        else:
            confidence = 0.92

        tags_to_apply = []
        brand_tag = get_or_create_brand_tag(manufacturer, db)
        tags_to_apply.append(
            {
                "tag_id": brand_tag.id,
                "source": "ai_classified",
                "confidence": confidence,
            }
        )

        if category and category != "Miscellaneous":
            commodity_tag = get_or_create_commodity_tag(category, db)
            if commodity_tag:
                tags_to_apply.append(
                    {
                        "tag_id": commodity_tag.id,
                        "source": "ai_classified",
                        "confidence": min(confidence, 0.95),
                    }
                )

        tag_material_card(card.id, tags_to_apply, db)

        if not card.manufacturer:
            card.manufacturer = manufacturer

        matched += 1

    db.commit()
    return matched, unknown

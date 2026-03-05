"""AI fallback classification — classify remaining untagged parts via Claude Haiku.

Last resort in the classification waterfall. Two modes:
  - Batch API (preferred): Submit all MPNs at once, 50% cheaper, no rate limits
  - Real-time API (fallback): Concurrent calls with rate limiting

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
- manufacturer: The manufacturer (full company name), or null if you are not at least 90% confident
- category: The component category (e.g., Microcontrollers (MCU), Capacitors, Connectors, etc.), or null if unknown
- confidence: Your confidence in the manufacturer identification (0.0 to 1.0). Only return a manufacturer if confidence >= 0.90.

Common MPN patterns to help:
- STM32*, STM8* → STMicroelectronics; TPS*, LM*, SN* → Texas Instruments
- ATMEGA*, PIC* → Microchip Technology; LPC*, S32K* → NXP Semiconductors
- IRF*, BSC*, CY8C* → Infineon Technologies; AD*, LTC*, MAX* → Analog Devices
- GRM*, BLM* → Murata; ERJ*, EEE* → Panasonic; CRCW* → Vishay

If this looks like an internal/custom part number (company-specific prefixes, purchase
order numbers, or non-standard formats), return null for manufacturer.
If you are less than 90% confident in the manufacturer, return null — do NOT guess.

Return a JSON array with one object per part:
[{{"mpn": "...", "manufacturer": "..." or null, "category": "..." or null, "confidence": 0.0-1.0}}]

Part numbers to classify:
{mpns}"""

_SYSTEM = "You are an expert electronic component classifier. Return only valid JSON. Use null instead of 'Unknown' when unsure."


async def classify_parts_with_ai(part_numbers: list[str]) -> list[dict]:  # pragma: no cover
    """Batch MPNs via Gradient (free) with Anthropic fallback. Parse structured JSON response."""
    from app.services.gradient_service import gradient_json

    mpn_list = "\n".join(f"- {mpn}" for mpn in part_numbers)
    prompt = _CLASSIFY_PROMPT.format(mpns=mpn_list)

    # Primary: Gradient (free, unlimited)
    result = await gradient_json(
        prompt,
        system=_SYSTEM,
        model_tier="default",
        max_tokens=4096,
        temperature=0.1,
        timeout=60,
    )

    # Fallback: direct Anthropic API ($400 budget)
    if not result or not isinstance(result, list):
        from app.utils.claude_client import claude_json

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
        confidence = 0.3 if is_unknown else 0.92

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


# ── Batch API mode ──────────────────────────────────────────────────────

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

        # custom_id encodes the card IDs for this batch
        card_ids = [str(row.id) for row in batch]
        custom_id = f"batch_{i // batch_size}"

        requests.append({
            "custom_id": custom_id,
            "prompt": prompt,
            "schema": _BATCH_SCHEMA,
            "system": _SYSTEM,
            "model_tier": "fast",
            "max_tokens": 4096,
        })

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

    # Persist metadata so we can process results later
    import json
    meta_path = "/tmp/ai_backfill_meta.json"
    with open(meta_path, "w") as f:
        json.dump({
            "batch_ids": batch_ids,
            "batch_meta": {k: v for k, v in batch_meta.items()},
            "total_mpns": len(untagged),
        }, f)

    logger.info(f"Batch metadata saved to {meta_path}")

    return {
        "batch_ids": batch_ids,
        "total_requests": len(requests),
        "total_mpns": len(untagged),
    }


async def check_and_apply_batch_results(db: Session) -> dict:  # pragma: no cover
    """Poll batch status and apply results if complete.

    Returns: {status, ...} where status is 'processing', 'complete', or 'error'
    """
    import json
    from app.utils.claude_client import claude_batch_results

    meta_path = "/tmp/ai_backfill_meta.json"
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
                        classified.append({
                            "mpn": (item.get("mpn") or "").strip(),
                            "manufacturer": (item.get("manufacturer") or "Unknown").strip(),
                            "category": (item.get("category") or "Miscellaneous").strip(),
                        })
            else:
                # Failed request — mark as unknown
                classified = [
                    {"mpn": mpn, "manufacturer": "Unknown", "category": "Miscellaneous"}
                    for _, mpn in batch
                ]

            matched, unknown = _apply_ai_results(classified, batch, db)
            total_matched += matched
            total_unknown += unknown
            total_processed += len(batch)

            if total_processed % 10000 == 0:
                db.commit()
                logger.info(f"Batch results: {total_processed}/{total_mpns} applied — {total_matched} matched")

        db.commit()

    if all_complete:
        logger.info(f"Batch AI backfill complete: {total_processed} processed, {total_matched} matched, {total_unknown} unknown")
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

        requests.append({
            "custom_id": f"backfill_{i}",
            "params": {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 8192,
                "system": _SYSTEM,
                "messages": [{"role": "user", "content": prompt}],
            },
        })

    # Submit to Batch API
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    # Write requests as JSONL
    import tempfile
    jsonl_lines = "\n".join(json.dumps(r) for r in requests)

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
        tags_to_apply.append({
            "tag_id": brand_tag.id,
            "source": "ai_classified",
            "confidence": confidence,
        })

        if category and category != "Miscellaneous":
            commodity_tag = get_or_create_commodity_tag(category, db)
            if commodity_tag:
                tags_to_apply.append({
                    "tag_id": commodity_tag.id,
                    "source": "ai_classified",
                    "confidence": min(confidence, 0.95),
                })

        tag_material_card(card.id, tags_to_apply, db)

        if not card.manufacturer:
            card.manufacturer = manufacturer

        matched += 1

    db.commit()
    return matched, unknown


# ── Internal part triage ───────────────────────────────────────────────

_TRIAGE_PROMPT = """For each part number below, determine whether it is:
1. A real electronic component MPN (manufacturer part number) — standard identifiers from semiconductor/passive/connector manufacturers
2. An internal/custom part number — company-specific codes, purchase order numbers, inventory codes, or non-standard formats

Indicators of internal part numbers:
- Contains company-specific prefixes or suffixes (e.g., "INT-", "CUST-", "PO-")
- Has unusual characters not typical for MPNs (underscores, equal signs, brackets)
- Pure numeric sequences without manufacturer patterns
- Very short (1-3 chars) or very long (>30 chars) strings
- Contains words like "SAMPLE", "TEST", "CUSTOM", "KIT", "ASSY"

Return a JSON array with one object per part:
[{{"mpn": "...", "is_internal": true/false, "reason": "brief explanation"}}]

Part numbers to classify:
{mpns}"""

_TRIAGE_SYSTEM = "You are an electronic component expert. Classify each part number as a real MPN or internal part number. Return only valid JSON."


def triage_internal_parts(mpns: list[str]) -> list[dict]:
    """Classify MPNs as real components vs internal part numbers using heuristics.

    Fast, no-API-call classification for obvious cases. Returns list of
    {mpn, is_internal, reason} dicts.

    Called by: app.services.tagging_ai.triage_batch
    """
    import re

    results = []
    for mpn in mpns:
        upper = mpn.upper().strip()
        is_internal = False
        reason = ""

        # Pure numeric
        if re.match(r"^\d+$", upper):
            is_internal = True
            reason = "pure numeric sequence"
        # Very short
        elif len(upper) <= 2:
            is_internal = True
            reason = "too short for standard MPN"
        # Contains obvious internal markers
        elif any(marker in upper for marker in ["INT-", "CUST-", "PO-", "PO#", "ASSY-", "KIT-", "SAMPLE", "TEST-"]):
            is_internal = True
            reason = "contains internal marker"
        # Contains unusual characters
        elif re.search(r"[=\[\]{}<>|\\]", upper):
            is_internal = True
            reason = "contains unusual characters"
        # Starts with special chars
        elif re.match(r"^[^A-Z0-9]", upper):
            is_internal = True
            reason = "starts with special character"
        # Very long
        elif len(upper) > 40:
            is_internal = True
            reason = "unusually long"

        results.append({"mpn": mpn, "is_internal": is_internal, "reason": reason})

    return results


async def submit_triage_batch(db: Session, limit: int = 50000) -> dict:
    """Triage untagged cards as real MPNs vs internal part numbers.

    Step 1: Heuristic pass (instant, no API) — catches obvious cases
    Step 2: Remaining ambiguous cards submitted to AI for classification

    Returns: {heuristic_flagged, ai_submitted, total_processed}
    """
    from app.models.tags import MaterialTag

    # Cards with NO MaterialTag AND not yet triaged
    tagged_ids = db.query(MaterialTag.material_card_id).distinct().subquery()
    candidates = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn)
        .filter(
            ~MaterialCard.id.in_(db.query(tagged_ids.c.material_card_id)),
            MaterialCard.is_internal_part.is_(False),
        )
        .order_by(MaterialCard.id)
        .limit(limit)
        .all()
    )

    if not candidates:
        return {"heuristic_flagged": 0, "ai_submitted": 0, "total_processed": 0}

    logger.info(f"Triage: processing {len(candidates)} untagged cards")

    # Step 1: Heuristic pass
    heuristic_flagged = 0
    remaining = []

    for card_id, mpn in candidates:
        results = triage_internal_parts([mpn])
        if results and results[0]["is_internal"]:
            card = db.get(MaterialCard, card_id)
            if card:
                card.is_internal_part = True
                heuristic_flagged += 1
        else:
            remaining.append((card_id, mpn))

        if heuristic_flagged % 1000 == 0 and heuristic_flagged > 0:
            db.commit()

    db.commit()
    logger.info(f"Triage heuristic pass: {heuristic_flagged} flagged as internal, {len(remaining)} remaining")

    # Step 2: AI triage for remaining ambiguous cards (using Batch API)
    ai_submitted = 0
    if remaining:
        from app.http_client import http
        from app.services.credential_service import get_credential_cached

        api_key = get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
        if api_key:
            # Build batch requests — 100 MPNs per request
            requests = []
            for i in range(0, len(remaining), 100):
                batch = remaining[i : i + 100]
                mpn_list = "\n".join(f"- {mpn}" for _, mpn in batch)
                prompt = _TRIAGE_PROMPT.format(mpns=mpn_list)

                requests.append({
                    "custom_id": f"triage_{i}",
                    "params": {
                        "model": "claude-haiku-4-5-20251001",
                        "max_tokens": 4096,
                        "system": _TRIAGE_SYSTEM,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                })

            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }

            resp = await http.post(
                "https://api.anthropic.com/v1/messages/batches",
                headers=headers,
                json={"requests": requests},
                timeout=60,
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                batch_id = data.get("id", "unknown")
                ai_submitted = len(remaining)
                logger.info(f"Triage AI batch submitted: batch_id={batch_id}, {ai_submitted} cards")
            else:
                logger.warning(f"Triage AI batch failed: HTTP {resp.status_code}")

    return {
        "heuristic_flagged": heuristic_flagged,
        "ai_submitted": ai_submitted,
        "total_processed": len(candidates),
    }


async def apply_triage_results(batch_id: str) -> dict:
    """Apply triage batch results — flag internal parts.

    Streams JSONL results and updates MaterialCard.is_internal_part.

    Returns: {total_lines, flagged, real_mpns, errors}
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

    # Check batch status
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

    # Stream results to temp file
    tmp_path = tempfile.mktemp(suffix=".jsonl", dir="/tmp")
    try:
        async with http.stream("GET", results_url, headers=headers, timeout=300) as stream:
            with open(tmp_path, "wb") as f:
                async for chunk in stream.aiter_bytes(chunk_size=65536):
                    f.write(chunk)
    except Exception as e:
        return {"error": f"Download failed: {e}"}

    db = SessionLocal()
    total_lines = 0
    flagged = 0
    real_mpns = 0
    errors = 0

    try:
        with open(tmp_path) as f:
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

                    message = result.get("message", {})
                    content_text = ""
                    for block in message.get("content", []):
                        if block.get("type") == "text":
                            content_text = block.get("text", "")
                            break

                    if not content_text:
                        errors += 1
                        continue

                    items = json.loads(content_text)
                    if not isinstance(items, list):
                        errors += 1
                        continue

                    for item in items:
                        mpn = (item.get("mpn") or "").strip().lower()
                        is_internal = item.get("is_internal", False)

                        if not mpn:
                            continue

                        card = db.query(MaterialCard).filter_by(normalized_mpn=mpn).first()
                        if card:
                            if is_internal:
                                card.is_internal_part = True
                                flagged += 1
                            else:
                                real_mpns += 1

                except (json.JSONDecodeError, KeyError):
                    errors += 1

                if total_lines % 500 == 0:
                    db.commit()

        db.commit()
    except Exception:
        logger.exception("Triage result apply failed")
        db.rollback()
    finally:
        db.close()
        import os
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    logger.info(
        f"Triage batch {batch_id} applied: {total_lines} lines, "
        f"{flagged} flagged internal, {real_mpns} real MPNs, {errors} errors"
    )
    return {"total_lines": total_lines, "flagged": flagged, "real_mpns": real_mpns, "errors": errors}

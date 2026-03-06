"""Internal part triage — classify MPNs as real components vs internal part numbers.

Two-step process:
  1. Heuristic pass (instant, no API) — catches obvious cases
  2. AI batch pass (Anthropic Batch API) — classifies ambiguous cases

Called by: app.routers.tagging_admin
Depends on: app.http_client, app.services.credential_service
"""

import json
import tempfile

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard

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

    Called by: app.services.tagging_ai_triage.submit_triage_batch
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

                requests.append(
                    {
                        "custom_id": f"triage_{i}",
                        "params": {
                            "model": "claude-haiku-4-5-20251001",
                            "max_tokens": 4096,
                            "system": _TRIAGE_SYSTEM,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                    }
                )

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

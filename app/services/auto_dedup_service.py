"""Auto-dedup service — background AI-driven vendor and company deduplication.

Runs daily via scheduler. Two-tier approach:
  - Score >= 98: auto-merge (near-certain duplicate)
  - Score 92-97: call Claude to confirm before merging

Key rule: dedup = merge data & add sites, never erase. All merges use the
extracted merge services which preserve alternate names, move sites, combine
tags, and append notes.

Note: Company duplicates with different owners/sites are intentionally allowed
(different salespeople may own different sites for the same customer), so the
auto-dedup only merges companies that have NO sites with assigned owners, or
where both companies share the same owner.

Called by: scheduler.py (job_auto_dedup)
Depends on: vendor_merge_service, company_merge_service, company_utils, claude_client
"""

from loguru import logger
from sqlalchemy.orm import Session

from ..models import VendorCard


def run_auto_dedup(db: Session) -> dict:
    """Run background dedup for both vendors and companies.

    Returns summary dict with merge counts.
    """
    stats = {"vendors_merged": 0, "companies_merged": 0}

    try:
        stats["vendors_merged"] = _dedup_vendors(db)
    except Exception:
        logger.exception("Vendor auto-dedup failed")
        db.rollback()

    try:
        stats["companies_merged"] = _dedup_companies(db)
    except Exception:
        logger.exception("Company auto-dedup failed")
        db.rollback()

    return stats


def _dedup_vendors(db: Session) -> int:
    """Find and merge duplicate vendor cards using fuzzy matching."""
    from .vendor_merge_service import merge_vendor_cards

    try:
        from thefuzz import fuzz
    except ImportError:
        logger.warning("thefuzz not installed — skipping vendor auto-dedup")
        return 0

    # Load vendor cards
    cards = (
        db.query(VendorCard.id, VendorCard.normalized_name, VendorCard.display_name, VendorCard.sighting_count)
        .filter(VendorCard.is_blacklisted.is_(False))
        .order_by(VendorCard.id)
        .limit(500)
        .all()
    )

    merged = 0
    merged_ids = set()
    seen_pairs = set()

    for i, a in enumerate(cards):
        if a.id in merged_ids:
            continue
        for b in cards[i + 1 :]:
            if b.id in merged_ids:
                continue
            pair_key = (min(a.id, b.id), max(a.id, b.id))
            if pair_key in seen_pairs:  # pragma: no cover — defensive; loop structure prevents repeats
                continue
            seen_pairs.add(pair_key)

            score = fuzz.token_sort_ratio(a.normalized_name or "", b.normalized_name or "")
            if score < 92:
                continue

            # Decide which to keep (more sightings wins)
            if (a.sighting_count or 0) >= (b.sighting_count or 0):
                keep_id, remove_id = a.id, b.id
            else:
                keep_id, remove_id = b.id, a.id

            should_merge = False
            if score >= 98:
                should_merge = True
                logger.info(
                    "Auto-merging vendors (score=%d): '%s' into '%s'",
                    score,
                    b.display_name if remove_id == b.id else a.display_name,
                    a.display_name if keep_id == a.id else b.display_name,
                )
            elif score >= 92:
                should_merge = _ai_confirm_vendor_merge(a.display_name, b.display_name, score)

            if should_merge:
                try:
                    merge_vendor_cards(keep_id, remove_id, db)
                    db.commit()
                    merged += 1
                    merged_ids.add(remove_id)
                except Exception:
                    logger.exception("Failed to merge vendors %d -> %d", remove_id, keep_id)
                    db.rollback()

            if merged >= 20:  # Cap merges per run
                break
        if merged >= 20:
            break

    return merged


def _dedup_companies(db: Session) -> int:
    """Find and merge duplicate companies using fuzzy matching.

    Respects the business rule that duplicate companies are allowed when
    different salespeople own sites — only merges when both companies have
    the same owner (or neither has one).
    """
    from ..company_utils import find_company_dedup_candidates
    from ..models import Company
    from .company_merge_service import merge_companies

    candidates = find_company_dedup_candidates(db, threshold=92, limit=50)
    merged = 0

    for c in candidates:
        keep_id = c["auto_keep_id"]
        remove_id = c["company_b"]["id"] if keep_id == c["company_a"]["id"] else c["company_a"]["id"]
        score = c["score"]

        # Check if different owners — if so, skip (intentional duplicates)
        keep = db.get(Company, keep_id)
        remove = db.get(Company, remove_id)
        if not keep or not remove:
            continue
        if keep.account_owner_id and remove.account_owner_id and keep.account_owner_id != remove.account_owner_id:
            continue  # Different owners — allowed duplicate

        should_merge = False
        if score >= 98:
            should_merge = True
            logger.info(
                "Auto-merging companies (score=%d): '%s' into '%s'",
                score,
                remove.name,
                keep.name,
            )
        elif score >= 92:
            should_merge = _ai_confirm_company_merge(keep.name, remove.name, keep.domain, remove.domain, score)

        if should_merge:
            try:
                merge_companies(keep_id, remove_id, db)
                db.commit()
                merged += 1
            except Exception:
                logger.exception("Failed to merge companies %d -> %d", remove_id, keep_id)
                db.rollback()

        if merged >= 10:  # Cap merges per run
            break

    return merged


def _ai_confirm_vendor_merge(name_a: str, name_b: str, score: int) -> bool:
    """Ask Claude if two vendor names are the same entity."""
    import asyncio

    try:
        return asyncio.get_event_loop().run_until_complete(
            _ask_claude_merge(
                f"Are these two vendor names the same company?\nA: {name_a}\nB: {name_b}\nFuzzy score: {score}%",
            )
        )
    except Exception:
        return False


def _ai_confirm_company_merge(name_a: str, name_b: str, domain_a: str | None, domain_b: str | None, score: int) -> bool:
    """Ask Claude if two company names are the same entity."""
    import asyncio

    prompt = (
        f"Are these two companies the same entity?\n"
        f"A: {name_a} (domain: {domain_a or 'unknown'})\n"
        f"B: {name_b} (domain: {domain_b or 'unknown'})\n"
        f"Fuzzy score: {score}%"
    )
    try:
        return asyncio.get_event_loop().run_until_complete(_ask_claude_merge(prompt))
    except Exception:
        return False


async def _ask_claude_merge(prompt: str) -> bool:
    """Generic Claude confirmation for merge — returns True if same entity."""
    from ..utils.claude_client import claude_structured

    schema = {
        "type": "object",
        "properties": {
            "same_entity": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["same_entity", "confidence"],
    }

    result = await claude_structured(
        prompt=prompt,
        schema=schema,
        system="You determine if two business names refer to the same entity. Be conservative — only confirm if very confident.",
        model_tier="fast",
        max_tokens=256,
    )

    if not result:
        return False
    return result.get("same_entity", False) and result.get("confidence", 0) >= 0.85

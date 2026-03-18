"""Sighting aggregation — builds vendor-level summaries from raw sightings.

Groups sightings by (vendor_name, requirement_id), computes aggregated qty
(AI-estimated or sum fallback), averaged price, best price, score (max),
and tier label. Summaries are materialized in VendorSightingSummary.

Called by: search_service._save_sightings() after sighting upsert
Depends on: VendorSightingSummary model, Sighting model, VendorCard model
"""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models.sourcing import Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard
from app.vendor_utils import normalize_vendor_name


def _score_to_tier(score: float | None) -> str:
    """Convert a 0-100 sighting score to a tier label."""
    if score is None:
        return "Poor"
    if score >= 70:
        return "Excellent"
    if score >= 40:
        return "Good"
    if score >= 20:
        return "Fair"
    return "Poor"


def _estimate_qty_with_ai(qty_values: list[int | None]) -> int | None:
    """Use Claude Haiku to estimate total available qty from varied listings.

    Returns estimated integer or None on failure.
    """
    non_null = [q for q in qty_values if q is not None]
    if not non_null:
        return None

    # For simple cases (all numeric), just sum — no AI needed
    if len(non_null) <= 2:
        return sum(non_null)

    try:
        from app.config import settings

        if not settings.ANTHROPIC_API_KEY:
            return sum(non_null)

        import anthropic

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        prompt = (
            f"Given these quantity listings from the same vendor for the same part: {non_null}. "
            f"Some may be duplicate stock listed on different platforms. "
            f"Estimate the total unique available inventory as a single integer. "
            f"Reply with ONLY the integer, nothing else."
        )
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        return int(text)
    except Exception:
        logger.warning("AI qty estimation failed, using sum fallback")
        return sum(non_null)


def rebuild_vendor_summaries(
    db: Session,
    requirement_id: int,
    vendor_names: list[str] | None = None,
) -> list[VendorSightingSummary]:
    """Rebuild VendorSightingSummary rows for given requirement + vendors.

    If vendor_names is None, rebuilds all vendors for that requirement.
    """
    query = db.query(Sighting).filter(
        Sighting.requirement_id == requirement_id,
        Sighting.is_unavailable.isnot(True),
    )
    if vendor_names:
        query = query.filter(Sighting.vendor_name.in_(vendor_names))

    sightings = query.all()

    # Group by vendor
    groups: dict[str, list[Sighting]] = {}
    for s in sightings:
        vn = (s.vendor_name or "unknown").lower().strip()
        groups.setdefault(vn, []).append(s)

    # Look up vendor phones in bulk
    vendor_phones: dict[str, str | None] = {}
    if groups:
        cards = (
            db.query(VendorCard.normalized_name, VendorCard.phones)
            .filter(VendorCard.normalized_name.in_(list(groups.keys())))
            .all()
        )
        for card in cards:
            phones = card.phones or []
            vendor_phones[card.normalized_name] = phones[0] if phones else None

    results = []
    for vn, group in groups.items():
        prices = [s.unit_price for s in group if s.unit_price is not None]
        qtys = [s.qty_available for s in group]
        scores = [s.score for s in group if s.score is not None]
        sources = list({s.source_type for s in group if s.source_type})

        max_score = max(scores) if scores else None
        avg_price = sum(prices) / len(prices) if prices else None
        best_price = min(prices) if prices else None
        estimated_qty = _estimate_qty_with_ai(qtys)
        if estimated_qty is None:
            non_null_qtys = [q for q in qtys if q is not None]
            estimated_qty = sum(non_null_qtys) if non_null_qtys else None

        # Upsert summary
        existing = db.query(VendorSightingSummary).filter_by(requirement_id=requirement_id, vendor_name=vn).first()
        if existing:
            existing.vendor_phone = vendor_phones.get(vn)
            existing.estimated_qty = estimated_qty
            existing.avg_price = round(avg_price, 4) if avg_price else None
            existing.best_price = round(best_price, 4) if best_price else None
            existing.listing_count = len(group)
            existing.source_types = sources
            existing.score = round(max_score, 1) if max_score else None
            existing.tier = _score_to_tier(max_score)
            existing.updated_at = datetime.now(timezone.utc)
            results.append(existing)
        else:
            summary = VendorSightingSummary(
                requirement_id=requirement_id,
                vendor_name=vn,
                vendor_phone=vendor_phones.get(vn),
                estimated_qty=estimated_qty,
                avg_price=round(avg_price, 4) if avg_price else None,
                best_price=round(best_price, 4) if best_price else None,
                listing_count=len(group),
                source_types=sources,
                score=round(max_score, 1) if max_score else None,
                tier=_score_to_tier(max_score),
                updated_at=datetime.now(timezone.utc),
            )
            db.add(summary)
            results.append(summary)

    db.flush()
    logger.info(
        "Rebuilt {} vendor summaries for requirement {}",
        len(results),
        requirement_id,
    )
    return results


def rebuild_vendor_summaries_from_sightings(
    db: Session,
    requirement_id: int,
    sightings: list,
) -> None:
    """Extract normalized vendor names from sightings and rebuild summaries.

    Silently catches errors so callers don't need try/except boilerplate.
    """
    try:
        vendor_names = list(
            {
                normalize_vendor_name(s.vendor_name)
                for s in sightings
                if s.vendor_name and normalize_vendor_name(s.vendor_name)
            }
        )
        if vendor_names:
            rebuild_vendor_summaries(db, requirement_id, vendor_names=vendor_names)
    except Exception:
        logger.debug("Vendor summary rebuild failed for requirement {}", requirement_id, exc_info=True)

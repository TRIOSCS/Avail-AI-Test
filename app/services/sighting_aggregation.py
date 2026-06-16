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


def _estimate_qty_with_ai(qty_values: list[int | None]) -> dict:
    """Use Claude Haiku to estimate total available qty from varied listings.

    Returns {"qty": int | None, "approximate": bool}.
    """
    non_null = [q for q in qty_values if q is not None]
    if not non_null:
        return {"qty": None, "approximate": False}

    # For simple cases (all numeric), just sum — no AI needed
    if len(non_null) <= 2:
        return {"qty": sum(non_null), "approximate": False}

    try:
        import anthropic

        from app.config import settings
        from app.utils.claude_client import MODELS

        if not settings.ANTHROPIC_API_KEY:
            return {"qty": max(non_null), "approximate": True}

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        prompt = (
            f"Given these quantity listings from the same vendor for the same part: {non_null}. "
            f"Some may be duplicate stock listed on different platforms. "
            f"Estimate the total unique available inventory as a single integer. "
            f"Reply with ONLY the integer, nothing else."
        )
        resp = client.messages.create(
            model=MODELS["fast"],
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        return {"qty": int(text), "approximate": False}
    except Exception:
        logger.warning("AI qty estimation failed, using max fallback")
        return {"qty": max(non_null), "approximate": True}


def rebuild_vendor_summaries(
    db: Session,
    requirement_id: int,
    vendor_names: list[str] | None = None,
) -> list[VendorSightingSummary]:
    """Rebuild VendorSightingSummary rows for the requirement.

    Pulls sightings via material_card_id set so prior searches on other requirements
    that share an MPN are visible. Falls back to requirement_id-direct sightings for
    rows missing material_card_id.
    """
    from app.models import MaterialCard, Requirement
    from app.utils.normalization import normalize_mpn_key

    req = db.get(Requirement, requirement_id)
    if not req:
        return []

    pns: list[str] = []
    if req.primary_mpn:
        pns.append(req.primary_mpn)
    for sub in req.substitutes or []:
        if isinstance(sub, dict):
            v = (sub.get("mpn") or "").strip()
        else:
            v = str(sub).strip() if sub else ""
        if v:
            pns.append(v)
    norm_keys = [k for k in (normalize_mpn_key(p) for p in pns) if k]

    card_ids: set[int] = set()
    if norm_keys:
        rows = db.query(MaterialCard.id).filter(MaterialCard.normalized_mpn.in_(norm_keys)).all()
        card_ids = {r[0] for r in rows}

    base_filter = [Sighting.is_unavailable.isnot(True)]
    if card_ids:
        base_filter.append(
            (Sighting.material_card_id.in_(card_ids))
            | ((Sighting.material_card_id.is_(None)) & (Sighting.requirement_id == requirement_id))
        )
    else:
        base_filter.append(Sighting.requirement_id == requirement_id)

    query = db.query(Sighting).filter(*base_filter)
    if vendor_names:
        query = query.filter(Sighting.vendor_name.in_(vendor_names))

    sightings = query.all()

    # Group by vendor
    groups: dict[str, list[Sighting]] = {}
    for s in sightings:
        vn = (s.vendor_name or "unknown").lower().strip()
        groups.setdefault(vn, []).append(s)

    # Look up vendor phones and card IDs in bulk
    vendor_phones: dict[str, str | None] = {}
    vendor_card_ids: dict[str, int] = {}
    if groups:
        cards = (
            db.query(VendorCard.id, VendorCard.normalized_name, VendorCard.phones)
            .filter(VendorCard.normalized_name.in_(list(groups.keys())))
            .all()
        )
        for card in cards:
            phones = card.phones or []
            vendor_phones[card.normalized_name] = phones[0] if phones else None
            vendor_card_ids[card.normalized_name] = card.id

    results = []
    for vn, group in groups.items():
        prices = [s.unit_price for s in group if s.unit_price is not None]
        qtys = [s.qty_available for s in group]
        scores = [s.score for s in group if s.score is not None]
        sources = list({s.source_type for s in group if s.source_type})

        max_score = max(scores) if scores else None
        avg_price = sum(prices) / len(prices) if prices else None
        best_price = min(prices) if prices else None
        qty_result = _estimate_qty_with_ai(qtys)
        estimated_qty = qty_result["qty"]
        if qty_result["approximate"]:
            logger.info("Approximate qty {} for vendor {} (AI fallback)", estimated_qty, vn)
        if estimated_qty is None:
            non_null_qtys = [q for q in qtys if q is not None]
            estimated_qty = max(non_null_qtys) if non_null_qtys else None

        # New pre-aggregated fields
        lead_times = [s.lead_time_days for s in group if s.lead_time_days is not None]
        moqs = [s.moq for s in group if s.moq is not None]
        newest = max((s.created_at for s in group if s.created_at), default=None)
        has_contact = any(s.vendor_email or s.vendor_phone for s in group) or bool(vendor_phones.get(vn))

        fields = {
            "vendor_phone": vendor_phones.get(vn),
            "estimated_qty": estimated_qty,
            "avg_price": round(avg_price, 4) if avg_price else None,
            "best_price": round(best_price, 4) if best_price else None,
            "listing_count": len(group),
            "source_types": sources,
            "score": round(max_score, 1) if max_score else None,
            "tier": _score_to_tier(max_score),
            "updated_at": datetime.now(timezone.utc),
            "vendor_card_id": vendor_card_ids.get(vn),
            "newest_sighting_at": newest,
            "best_lead_time_days": min(lead_times) if lead_times else None,
            "min_moq": min(moqs) if moqs else None,
            "has_contact_info": has_contact,
        }

        # Upsert summary
        existing = db.query(VendorSightingSummary).filter_by(requirement_id=requirement_id, vendor_name=vn).first()
        if existing:
            for key, value in fields.items():
                setattr(existing, key, value)
            results.append(existing)
        else:
            summary = VendorSightingSummary(requirement_id=requirement_id, vendor_name=vn, **fields)
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
    """Rebuild all vendor summaries for the requirement when new sightings land.

    Silently catches errors so callers don't need try/except boilerplate.
    """
    try:
        # Skip cheaply when no sighting carries a usable vendor_name; otherwise
        # always rebuild ALL vendors for the requirement. The previous
        # implementation passed normalize_vendor_name(...) names into
        # rebuild_vendor_summaries(vendor_names=...), which filters
        # Sighting.vendor_name (raw, mixed-case, with suffixes) via IN — so
        # only vendors whose raw name happened to equal the normalized form
        # (e.g. "element14") ever produced summary rows.
        has_vendor = any(s.vendor_name and s.vendor_name.strip() for s in sightings)
        if has_vendor:
            rebuild_vendor_summaries(db, requirement_id)
    except Exception:
        logger.warning("Vendor summary rebuild failed for requirement {}", requirement_id, exc_info=True)

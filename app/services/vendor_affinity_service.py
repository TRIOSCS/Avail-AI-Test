"""
Vendor Affinity Service — finds vendors likely to supply a given MPN.
What: Three-level affinity matching (L1: same manufacturer, L2: same commodity, L3: AI classification)
Called by: app/search_service.py during search fan-out
Depends on: app.models (MaterialCard, Sighting, MaterialVendorHistory, EntityTag, Tag, VendorCard), Claude API for L3
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    EntityTag,
    MaterialCard,
    MaterialVendorHistory,
    Sighting,
    Tag,
    VendorCard,
)


def find_affinity_vendors_l1(mpn: str, db: Session) -> list[dict]:
    """Find vendors who supply other MPNs from the same manufacturer as the target MPN."""
    normalized = mpn.strip().lower()
    card = db.query(MaterialCard).filter(MaterialCard.normalized_mpn == normalized).first()
    if not card or not card.manufacturer:
        logger.debug("L1: no MaterialCard or manufacturer for MPN={}", mpn)
        return []

    manufacturer = card.manufacturer

    # Find vendors who have supplied OTHER MPNs from the same manufacturer via MaterialVendorHistory.
    # Join MaterialVendorHistory -> MaterialCard to filter by manufacturer, excluding the target MPN.
    rows = (
        db.query(
            MaterialVendorHistory.vendor_name_normalized,
            MaterialVendorHistory.vendor_name,
            func.count(func.distinct(MaterialCard.normalized_mpn)).label("mpn_count"),
        )
        .join(MaterialCard, MaterialVendorHistory.material_card_id == MaterialCard.id)
        .filter(
            MaterialCard.manufacturer == manufacturer,
            MaterialCard.normalized_mpn != normalized,
        )
        .group_by(
            MaterialVendorHistory.vendor_name_normalized,
            MaterialVendorHistory.vendor_name,
        )
        .order_by(func.count(func.distinct(MaterialCard.normalized_mpn)).desc())
        .limit(20)
        .all()
    )

    results = []
    for row in rows:
        vendor_norm = row.vendor_name_normalized or row.vendor_name.lower()
        vc = db.query(VendorCard).filter(VendorCard.normalized_name == vendor_norm).first()
        results.append(
            {
                "vendor_name": row.vendor_name,
                "vendor_id": vc.id if vc else None,
                "mpn_count": row.mpn_count,
                "manufacturer": manufacturer,
                "level": 1,
                "confidence": 0.0,
            }
        )

    logger.info("L1: found {} vendors for manufacturer={} (MPN={})", len(results), manufacturer, mpn)
    return results


def find_affinity_vendors_l2(
    mpn: str, db: Session, exclude_vendors: set[str] | None = None
) -> list[dict]:
    """Find vendors that share commodity tags with the target MPN's vendor cards."""
    normalized = mpn.strip().lower()
    card = db.query(MaterialCard).filter(MaterialCard.normalized_mpn == normalized).first()
    if not card:
        logger.debug("L2: no MaterialCard for MPN={}", mpn)
        return []

    # Find commodity tag IDs linked to this MaterialCard's vendor cards via EntityTag.
    # First get vendor cards that have sightings for this MPN.
    vendor_card_ids = (
        db.query(VendorCard.id)
        .join(Sighting, Sighting.vendor_name_normalized == VendorCard.normalized_name)
        .filter(Sighting.normalized_mpn == normalized)
        .distinct()
        .all()
    )
    vc_ids = [row[0] for row in vendor_card_ids]
    if not vc_ids:
        logger.debug("L2: no VendorCards linked to MPN={}", mpn)
        return []

    # Find commodity tags on those vendor cards.
    commodity_tag_ids = (
        db.query(EntityTag.tag_id)
        .join(Tag, EntityTag.tag_id == Tag.id)
        .filter(
            EntityTag.entity_type == "vendor_card",
            EntityTag.entity_id.in_(vc_ids),
            Tag.tag_type == "commodity",
        )
        .distinct()
        .all()
    )
    tag_ids = [row[0] for row in commodity_tag_ids]
    if not tag_ids:
        logger.debug("L2: no commodity tags for MPN={}", mpn)
        return []

    # Find other vendor cards that share those commodity tags.
    exclude = exclude_vendors or set()
    other_vendors = (
        db.query(
            EntityTag.entity_id,
            func.count(EntityTag.tag_id).label("tag_count"),
        )
        .filter(
            EntityTag.entity_type == "vendor_card",
            EntityTag.tag_id.in_(tag_ids),
            ~EntityTag.entity_id.in_(vc_ids),
        )
        .group_by(EntityTag.entity_id)
        .order_by(func.count(EntityTag.tag_id).desc())
        .limit(20)
        .all()
    )

    results = []
    for row in other_vendors:
        vc = db.query(VendorCard).filter(VendorCard.id == row.entity_id).first()
        if not vc:
            continue
        if vc.normalized_name in exclude:
            continue
        results.append(
            {
                "vendor_name": vc.display_name,
                "vendor_id": vc.id,
                "mpn_count": row.tag_count,
                "manufacturer": card.manufacturer,
                "level": 2,
                "confidence": 0.0,
            }
        )

    logger.info("L2: found {} vendors via commodity tags for MPN={}", len(results), mpn)
    return results


def find_affinity_vendors_l3(
    mpn: str, manufacturer: str | None, db: Session
) -> list[dict]:
    """Use Claude to classify MPN into a category, then find vendors supplying that category."""
    api_key = settings.anthropic_api_key
    if not api_key:
        logger.debug("L3: no anthropic_api_key configured, skipping")
        return []

    # Classify the MPN into a sourcing category using Claude Haiku.
    category = _classify_mpn(mpn, manufacturer, api_key)
    if not category:
        return []

    # Query sightings for vendors who supplied MPNs in that category.
    rows = (
        db.query(
            Sighting.vendor_name_normalized,
            Sighting.vendor_name,
            func.count(func.distinct(Sighting.normalized_mpn)).label("mpn_count"),
        )
        .join(MaterialCard, Sighting.material_card_id == MaterialCard.id)
        .filter(MaterialCard.category.ilike(f"%{category}%"))
        .group_by(Sighting.vendor_name_normalized, Sighting.vendor_name)
        .order_by(func.count(func.distinct(Sighting.normalized_mpn)).desc())
        .limit(20)
        .all()
    )

    results = []
    for row in rows:
        vendor_norm = row.vendor_name_normalized or row.vendor_name.lower()
        vc = db.query(VendorCard).filter(VendorCard.normalized_name == vendor_norm).first()
        results.append(
            {
                "vendor_name": row.vendor_name,
                "vendor_id": vc.id if vc else None,
                "mpn_count": row.mpn_count,
                "manufacturer": manufacturer,
                "level": 3,
                "confidence": 0.0,
            }
        )

    logger.info("L3: found {} vendors for category={} (MPN={})", len(results), category, mpn)
    return results


def _classify_mpn(mpn: str, manufacturer: str | None, api_key: str) -> str | None:
    """Call Claude Haiku to classify an MPN into a broad sourcing category."""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        mfr_hint = f" from {manufacturer}" if manufacturer else ""
        message = client.messages.create(
            model="claude-haiku-4-20250514",
            max_tokens=50,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Classify this electronic component MPN into ONE broad sourcing "
                        f"category (e.g. Microcontroller, Capacitor, Resistor, Connector, "
                        f"Memory, Power IC, Sensor, FPGA, etc). "
                        f"MPN: {mpn}{mfr_hint}. "
                        f"Reply with ONLY the category name, nothing else."
                    ),
                }
            ],
        )
        category = message.content[0].text.strip()
        logger.info("L3: classified MPN={} as category={}", mpn, category)
        return category
    except Exception:
        logger.exception("L3: failed to classify MPN={}", mpn)
        return None


def score_affinity_matches(mpn: str, matches: list[dict]) -> list[dict]:
    """Assign confidence scores and reasoning to affinity matches using deterministic scoring."""
    if not matches:
        return []

    scored = []
    for match in matches:
        level = match["level"]
        mpn_count = match.get("mpn_count", 1)
        extra = max(0, mpn_count - 1)

        if level == 1:
            confidence = 0.50 + 0.025 * extra
            confidence = min(confidence, 0.75)
            reason = (
                f"Vendor supplied {mpn_count} other MPN(s) from {match.get('manufacturer', 'same manufacturer')}"
            )
        elif level == 2:
            confidence = 0.40 + 0.02 * extra
            confidence = min(confidence, 0.60)
            reason = f"Vendor shares commodity tags ({mpn_count} matching tag(s))"
        else:
            confidence = 0.30 + 0.02 * extra
            confidence = min(confidence, 0.50)
            reason = f"Vendor supplies parts in the same AI-classified category ({mpn_count} MPN(s))"

        # Clamp to [0.30, 0.75]
        confidence = max(0.30, min(0.75, confidence))

        scored_match = {**match, "confidence": round(confidence, 4), "reasoning": reason}
        scored.append(scored_match)

    return scored


def find_vendor_affinity(mpn: str, db: Session) -> list[dict]:
    """Top-level orchestrator: run L1+L2 (and L3 if needed), score, deduplicate, return top 10."""
    l1 = find_affinity_vendors_l1(mpn, db)
    l2_exclude = {m["vendor_name"].lower() for m in l1}
    l2 = find_affinity_vendors_l2(mpn, db, exclude_vendors=l2_exclude)

    combined = l1 + l2

    if len(combined) < 5:
        card = db.query(MaterialCard).filter(
            MaterialCard.normalized_mpn == mpn.strip().lower()
        ).first()
        manufacturer = card.manufacturer if card else None
        l3_exclude = {m["vendor_name"].lower() for m in combined}
        l3 = find_affinity_vendors_l3(mpn, manufacturer, db)
        l3 = [m for m in l3 if m["vendor_name"].lower() not in l3_exclude]
        combined.extend(l3)

    scored = score_affinity_matches(mpn, combined)

    # Deduplicate: keep highest confidence per vendor_name (lowered).
    best: dict[str, dict] = {}
    for match in scored:
        key = match["vendor_name"].lower()
        if key not in best or match["confidence"] > best[key]["confidence"]:
            best[key] = match

    deduped = sorted(best.values(), key=lambda m: m["confidence"], reverse=True)
    result = deduped[:10]

    logger.info("find_vendor_affinity: MPN={}, returning {} matches", mpn, len(result))
    return result

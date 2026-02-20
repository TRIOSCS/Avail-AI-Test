"""services/vendor_analysis_service.py -- Vendor material analysis (extracted from routers/vendors.py).

Avoids circular imports: deep_enrichment_service needs _analyze_vendor_materials
but should not import from routers.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func as sqlfunc

from ..models import MaterialCard, MaterialVendorHistory, Sighting, VendorCard


async def _analyze_vendor_materials(card_id: int, db_session=None):
    """Analyze a vendor's MaterialVendorHistory to generate brand and commodity tags.

    If db_session is None, creates its own session (for background use).
    """
    from ..database import SessionLocal
    from ..utils.claude_client import claude_json

    own_session = db_session is None
    db = db_session or SessionLocal()
    try:
        card = db.get(VendorCard, card_id)
        if not card:
            return

        # Fetch parts from both MaterialVendorHistory and Sightings
        parts_list = []
        seen_mpns = set()

        # 1. MaterialVendorHistory (long-term tracked)
        mvh_rows = (
            db.query(MaterialVendorHistory, MaterialCard)
            .join(
                MaterialCard, MaterialVendorHistory.material_card_id == MaterialCard.id
            )
            .filter(MaterialVendorHistory.vendor_name == card.normalized_name)
            .order_by(MaterialVendorHistory.times_seen.desc())
            .limit(150)
            .all()
        )
        for mvh, mc in mvh_rows:
            key = (mc.display_mpn or "").lower()
            if key and key not in seen_mpns:
                seen_mpns.add(key)
                parts_list.append(
                    f"{mc.display_mpn} — {mvh.last_manufacturer or mc.manufacturer or 'unknown'}"
                )

        # 2. Sightings (search results) — fill remaining slots
        sighting_rows = (
            db.query(Sighting.mpn_matched, Sighting.manufacturer)
            .filter(
                sqlfunc.lower(sqlfunc.trim(Sighting.vendor_name))
                == card.normalized_name
            )
            .filter(Sighting.mpn_matched.isnot(None), Sighting.mpn_matched != "")
            .order_by(Sighting.created_at.desc())
            .limit(200)
            .all()
        )
        for mpn, mfr in sighting_rows:
            key = (mpn or "").lower()
            if key and key not in seen_mpns:
                seen_mpns.add(key)
                parts_list.append(f"{mpn} — {mfr or 'unknown'}")
            if len(parts_list) >= 200:
                break

        if not parts_list:
            return

        prompt = (
            f"Analyze this vendor's part inventory to identify their specialties.\n\n"
            f"Vendor: {card.display_name}\n"
            f"Parts they carry ({len(parts_list)} samples):\n"
            + "\n".join(parts_list[:200])
            + "\n\n"
            "Return JSON with two arrays — ONLY include items that appear multiple times "
            "or show a clear concentration/specialty. Do NOT list everything, only genuine focus areas.\n"
            '- "brands": brands/manufacturers this vendor clearly specializes in '
            "(must appear in at least 2-3 parts to qualify). Max 5.\n"
            '- "commodities": commodity categories they concentrate on '
            '(e.g., "Server", "Networking", "Storage", "Memory", "Display"). '
            "Only categories with multiple parts. Max 5.\n\n"
            "If the data is too sparse to identify specialties, return empty arrays.\n"
            "Return ONLY the JSON object, no explanation."
        )

        result = await claude_json(
            prompt,
            system="You identify vendor specialties in electronic components and IT hardware. "
            "Only flag genuine concentrations — if a vendor has 1 IBM part, that is NOT an IBM specialty. "
            "Be conservative: empty arrays are better than inaccurate tags.",
            model_tier="fast",
            max_tokens=512,
        )

        if not result or not isinstance(result, dict):
            return

        brands = result.get("brands", [])
        commodities = result.get("commodities", [])

        # Validate: must be lists of strings
        if isinstance(brands, list):
            card.brand_tags = [str(b).strip() for b in brands if b][:5]
        if isinstance(commodities, list):
            card.commodity_tags = [str(c).strip() for c in commodities if c][:5]
        card.material_tags_updated_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "Material tags updated for vendor %s (card %d): %d brands, %d commodities",
            card.display_name,
            card_id,
            len(card.brand_tags),
            len(card.commodity_tags),
        )
    except Exception:
        logger.exception("Material analysis failed for vendor card %d", card_id)
        if own_session:
            db.rollback()
    finally:
        if own_session:
            db.close()

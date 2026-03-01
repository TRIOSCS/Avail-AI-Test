"""services/customer_analysis_service.py -- Customer material analysis.

Analyzes a customer's requisition history to identify brand and commodity
concentrations, mirroring the vendor_analysis_service pattern.
"""

from datetime import datetime, timezone

from loguru import logger

from ..models import Company, CustomerSite, Requirement, Requisition, Sighting


async def analyze_customer_materials(company_id: int, db_session=None):
    """Analyze a customer's requisition history to generate brand/commodity tags.

    If db_session is None, creates its own session (for background use).
    """
    from ..database import SessionLocal
    from ..utils.claude_client import claude_json

    own_session = db_session is None
    db = db_session or SessionLocal()
    try:
        company = db.get(Company, company_id)
        if not company:
            return

        # Get all site IDs for this company
        site_ids = [
            s.id
            for s in db.query(CustomerSite.id)
            .filter(CustomerSite.company_id == company_id)
            .all()
        ]
        if not site_ids:
            return

        parts_list = []
        seen_mpns = set()

        # 1. Requirements (brand field) from requisitions linked to company sites
        req_rows = (
            db.query(Requirement.primary_mpn, Requirement.brand)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(Requisition.customer_site_id.in_(site_ids))
            .filter(Requirement.primary_mpn.isnot(None), Requirement.primary_mpn != "")
            .order_by(Requisition.created_at.desc())
            .limit(200)
            .all()
        )
        for mpn, brand in req_rows:
            key = (mpn or "").lower()
            if key and key not in seen_mpns:
                seen_mpns.add(key)
                parts_list.append(f"{mpn} — {brand or 'unknown'}")

        # 2. Sightings (manufacturer field) from those same requisitions
        sighting_rows = (
            db.query(Sighting.mpn_matched, Sighting.manufacturer)
            .join(Requirement, Sighting.requirement_id == Requirement.id)
            .join(Requisition, Requirement.requisition_id == Requisition.id)
            .filter(Requisition.customer_site_id.in_(site_ids))
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
            f"Analyze this customer's part purchasing history to identify their focus areas.\n\n"
            f"Customer: {company.name}\n"
            f"Parts they've requested ({len(parts_list)} samples):\n"
            + "\n".join(parts_list[:200])
            + "\n\n"
            "Return JSON with two arrays — ONLY include items that appear multiple times "
            "or show a clear concentration/focus. Do NOT list everything, only genuine focus areas.\n"
            '- "brands": brands/manufacturers this customer clearly focuses on '
            "(must appear in at least 2-3 parts to qualify). Max 5.\n"
            '- "commodities": commodity categories they concentrate on '
            '(e.g., "Server", "Networking", "Storage", "Memory", "Display"). '
            "Only categories with multiple parts. Max 5.\n\n"
            "If the data is too sparse to identify focus areas, return empty arrays.\n"
            "Return ONLY the JSON object, no explanation."
        )

        result = await claude_json(
            prompt,
            system="You identify customer purchasing patterns in electronic components and IT hardware. "
            "Only flag genuine concentrations — if a customer requested 1 IBM part, that is NOT an IBM focus. "
            "Be conservative: empty arrays are better than inaccurate tags.",
            model_tier="fast",
            max_tokens=512,
        )

        if not result or not isinstance(result, dict):
            return

        brands = result.get("brands", [])
        commodities = result.get("commodities", [])

        if isinstance(brands, list):
            company.brand_tags = [str(b).strip() for b in brands if b][:5]
        if isinstance(commodities, list):
            company.commodity_tags = [str(c).strip() for c in commodities if c][:5]
        company.material_tags_updated_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "Material tags updated for customer %s (id %d): %d brands, %d commodities",
            company.name,
            company_id,
            len(company.brand_tags),
            len(company.commodity_tags),
        )
    except Exception:
        logger.exception("Material analysis failed for company %d", company_id)
        if own_session:
            db.rollback()
    finally:
        if own_session:
            db.close()

"""services/vendor_analysis_service.py -- Vendor material analysis (extracted from routers/vendors.py).

Avoids circular imports: deep_enrichment_service needs _analyze_vendor_materials
but should not import from routers.
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy import text as sqltext
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from ..cache.decorators import cached_endpoint
from ..models import MaterialCard, MaterialVendorHistory, Sighting, VendorCard, VendorReview
from ..models.strategic import StrategicVendor
from ..models.vendors import VendorContact
from ..utils.search_builder import SearchBuilder


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
            .join(MaterialCard, MaterialVendorHistory.material_card_id == MaterialCard.id)
            .filter(
                MaterialVendorHistory.vendor_name == card.normalized_name,
                MaterialCard.deleted_at.is_(None),
            )
            .order_by(MaterialVendorHistory.times_seen.desc())
            .limit(150)
            .all()
        )
        for mvh, mc in mvh_rows:
            key = (mc.display_mpn or "").lower()
            if key and key not in seen_mpns:
                seen_mpns.add(key)
                parts_list.append(f"{mc.display_mpn} — {mvh.last_manufacturer or mc.manufacturer or 'unknown'}")

        # 2. Sightings (search results) — fill remaining slots
        sighting_rows = (
            db.query(Sighting.mpn_matched, Sighting.manufacturer)
            .filter(Sighting.vendor_name_normalized == card.normalized_name)
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
            f"Parts they carry ({len(parts_list)} samples):\n" + "\n".join(parts_list[:200]) + "\n\n"
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
        card.material_tags_updated_at = datetime.now(UTC)
        db.commit()

        logger.info(
            "Material tags updated for vendor {} (card {}): {} brands, {} commodities",
            card.display_name,
            card_id,
            len(card.brand_tags),
            len(card.commodity_tags),
        )
    except Exception:
        logger.exception("Material analysis failed for vendor card {}", card_id)
        if own_session:
            db.rollback()
    finally:
        if own_session:
            db.close()


@cached_endpoint(
    prefix="vendor_list", ttl_hours=0.5, key_params=["q", "tag", "tier", "sort", "order", "limit", "offset"]
)
def list_vendor_cards(q, tag, tier, sort, order, limit, offset, db: Session):
    """Vendor-cards list projection: tier filter, tag/name/FTS search, sort,
    pagination, and batched review/claim/top-contact stats (cached 30 min).

    Tier bands (vendor_score): proven >= 66, developing 33-66, caution < 33 (all
    established vendors only); "new" = is_new_vendor or unscored. When no manual
    reviews exist, a display star rating is auto-derived from response rate, win
    rate, and vendor score. Moved from routers/vendors_crud.list_vendors (the
    route delegates here); *q* arrives already stripped/lowercased.
    """
    query = db.query(VendorCard)

    # ── Tier filter ──
    if tier:
        tier = tier.strip().lower()
        if tier == "proven":
            query = query.filter(
                VendorCard.vendor_score.isnot(None),
                VendorCard.vendor_score >= 66,
                VendorCard.is_new_vendor.is_(False),
            )
        elif tier == "developing":
            query = query.filter(
                VendorCard.vendor_score.isnot(None),
                VendorCard.vendor_score >= 33,
                VendorCard.vendor_score < 66,
                VendorCard.is_new_vendor.is_(False),
            )
        elif tier == "caution":
            query = query.filter(
                VendorCard.vendor_score.isnot(None),
                VendorCard.vendor_score < 33,
                VendorCard.is_new_vendor.is_(False),
            )
        elif tier == "new":
            query = query.filter(
                sqlfunc.coalesce(VendorCard.is_new_vendor, True).is_(True) | VendorCard.vendor_score.is_(None)
            )

    # ── Default order ──
    query = query.order_by(VendorCard.display_name)
    if tag.strip():
        from sqlalchemy import String as SAString

        safe_tag = tag.strip().lower()
        query = query.filter(
            sqlfunc.lower(sqlfunc.cast(VendorCard.brand_tags, SAString)).contains(safe_tag)
            | sqlfunc.lower(sqlfunc.cast(VendorCard.commodity_tags, SAString)).contains(safe_tag)
        )
    if q:
        from sqlalchemy import String as SAString
        from sqlalchemy import or_

        sb = SearchBuilder(q)
        # Tag match — vendors whose brand/commodity tags contain the query
        tag_filter = or_(
            sqlfunc.lower(sqlfunc.cast(VendorCard.brand_tags, SAString)).contains(q),
            sqlfunc.lower(sqlfunc.cast(VendorCard.commodity_tags, SAString)).contains(q),
            sb.ilike_filter(VendorCard.industry),
        )
        name_filter = sb.ilike_filter(VendorCard.normalized_name)

        if len(q) >= 3:
            # Full-text search for longer queries (faster + ranked)
            try:
                fts_query = (
                    db.query(VendorCard)
                    .filter(
                        VendorCard.search_vector.isnot(None),
                        sqltext("search_vector @@ plainto_tsquery('english', :q)"),
                    )
                    .params(q=q)
                    .order_by(
                        sqltext("ts_rank(search_vector, plainto_tsquery('english', :q)) DESC"),
                    )
                    .params(q=q)
                )
                fts_count = fts_query.count()
                if fts_count > 0:
                    query = fts_query
                else:
                    # FTS found nothing, fall back to name + tag search
                    query = query.filter(or_(name_filter, tag_filter))
            except (ProgrammingError, OperationalError):
                # FTS not available (e.g., SQLite in tests), fall back
                query = query.filter(or_(name_filter, tag_filter))
        else:
            query = query.filter(or_(name_filter, tag_filter))
    # ── Apply explicit sort (overrides default order_by) ──
    if sort:
        sort = sort.strip().lower()
        sort_map = {
            "name": VendorCard.display_name,
            "score": VendorCard.vendor_score,
            "sighting_count": VendorCard.sighting_count,
            "response_rate": VendorCard.total_responses,  # proxy: sort by raw responses
            "total_pos": VendorCard.total_pos,
        }
        sort_col = sort_map.get(sort)
        if sort_col is not None:
            if order.strip().lower() == "desc":
                query = query.order_by(None).order_by(sort_col.desc().nullslast())
            else:
                query = query.order_by(None).order_by(sort_col.asc().nullsfirst())

    total = query.count()
    if offset and offset >= total:
        # Stale offset beyond the (re)filtered result set — e.g. a bookmarked or
        # hand-edited URL. Never blindly trust a round-tripped offset: snap back
        # to page 1 (same clamp as crm_service.customer_contacts_context).
        offset = 0
    cards = query.limit(limit).offset(offset).all()
    if not cards:
        return {"vendors": [], "total": total, "limit": limit, "offset": offset}
    # card_ids is non-empty here -- the empty-cards case returned above.
    card_ids = [c.id for c in cards]
    # Batch fetch review stats -- single query instead of N+1
    review_stats = {}
    for cid, avg, cnt in (
        db.query(
            VendorReview.vendor_card_id,
            sqlfunc.avg(VendorReview.rating),
            sqlfunc.count(VendorReview.id),
        )
        .filter(VendorReview.vendor_card_id.in_(card_ids))
        .group_by(VendorReview.vendor_card_id)
        .all()
    ):
        review_stats[cid] = (avg, cnt)
    # Batch fetch strategic claim info -- single query instead of N+1
    claim_map = {}
    for sv in (
        db.query(StrategicVendor)
        .filter(
            StrategicVendor.vendor_card_id.in_(card_ids),
            StrategicVendor.released_at.is_(None),
        )
        .all()
    ):
        owner_name = sv.user.name if sv.user else None
        claim_map[sv.vendor_card_id] = {
            "claimed_by_user_id": sv.user_id,
            "claimed_by_name": owner_name,
        }

    # Batch fetch top contact per vendor -- single query, dedup in Python
    top_contact_map = {}
    contacts = (
        db.query(VendorContact)
        .filter(VendorContact.vendor_card_id.in_(card_ids))
        .order_by(
            VendorContact.relationship_score.desc().nullslast(),
            VendorContact.interaction_count.desc().nullslast(),
            VendorContact.last_seen_at.desc().nullslast(),
        )
        .all()
    )
    for vc in contacts:
        if vc.vendor_card_id not in top_contact_map:
            top_contact_map[vc.vendor_card_id] = {
                "name": vc.full_name,
                "email": vc.email,
                "phone": vc.phone,
            }

    results = []
    for c in cards:
        stat = review_stats.get(c.id)
        avg_rating = round(float(stat[0]), 1) if stat else None
        review_count = int(stat[1]) if stat else 0
        rating_source = "manual" if stat else None
        resp_rate = None
        if c.total_outreach and c.total_outreach > 0:
            resp_rate = round((c.total_responses or 0) / c.total_outreach * 100, 1)

        # Auto-calculated star rating baseline when no manual reviews
        if avg_rating is None:
            auto_score = 0
            components = 0
            if resp_rate is not None:
                auto_score += (resp_rate / 100) * 5
                components += 1
            if c.overall_win_rate is not None:
                auto_score += c.overall_win_rate * 5
                components += 1
            if c.vendor_score is not None:
                auto_score += (c.vendor_score / 100) * 5
                components += 1
            if components > 0:
                avg_rating = round(auto_score / components, 1)
                rating_source = "auto"

        # Build location string from available fields
        loc_parts = [p for p in [c.hq_city, c.hq_state, c.hq_country] if p]
        location = ", ".join(loc_parts) if loc_parts else None

        claim = claim_map.get(c.id)
        results.append(
            {
                "id": c.id,
                "display_name": c.display_name,
                "emails": c.emails or [],
                "phones": c.phones or [],
                "sighting_count": c.sighting_count or 0,
                "vendor_score": c.vendor_score,
                "is_new_vendor": c.is_new_vendor if c.is_new_vendor is not None else True,
                "engagement_score": c.vendor_score,
                "is_blacklisted": c.is_blacklisted or False,
                "avg_rating": avg_rating,
                "review_count": review_count,
                "total_pos": c.total_pos or 0,
                "response_rate": resp_rate,
                "last_sighting_at": (c.last_activity_at or c.updated_at or c.created_at).isoformat()
                if (c.last_activity_at or c.updated_at or c.created_at)
                else None,
                "brand_tags": c.brand_tags or [],
                "commodity_tags": c.commodity_tags or [],
                "industry": c.industry,
                "location": location,
                "website": c.website,
                "domain": c.domain,
                "avg_response_hours": c.avg_response_hours,
                "overall_win_rate": c.overall_win_rate,
                "total_revenue": c.total_revenue or 0,
                "claimed_by": claim,
                "top_contact": top_contact_map.get(c.id),
                "rating_source": rating_source,
            }
        )
    return {"vendors": results, "total": total, "limit": limit, "offset": offset}

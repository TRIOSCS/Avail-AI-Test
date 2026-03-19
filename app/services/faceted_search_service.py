"""Faceted search query service.

What: Builds faceted queries on material_cards + material_spec_facets.
      Provides commodity counts, facet counts, sub-filter options.
Called by: htmx_views.py faceted search routes
Depends on: MaterialCard, MaterialSpecFacet, CommoditySpecSchema
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet


def get_commodity_counts(db: Session) -> dict[str, int]:
    """Return {commodity_key: count} for all non-deleted material cards."""
    rows = (
        db.query(
            func.lower(func.trim(MaterialCard.category)),
            func.count(MaterialCard.id),
        )
        .filter(MaterialCard.deleted_at.is_(None), MaterialCard.category.isnot(None))
        .group_by(func.lower(func.trim(MaterialCard.category)))
        .all()
    )
    return {cat: count for cat, count in rows if cat}


def get_facet_counts(
    db: Session,
    commodity: str,
    active_filters: dict | None = None,
) -> dict[str, dict[str, int]]:
    """Return facet value counts for a commodity.

    Returns: {spec_key: {value: count, ...}, ...}
    Only includes text-based facets (enums, booleans).
    """
    commodity = commodity.lower().strip()

    base_q = db.query(MaterialSpecFacet.material_card_id).filter(
        MaterialSpecFacet.category == commodity,
    )

    # Apply active filters to narrow the base set
    if active_filters:
        for key, values in active_filters.items():
            if key.endswith("_min") or key.endswith("_max"):
                continue  # Range filters handled separately
            if isinstance(values, list) and values:
                base_q = base_q.filter(
                    MaterialSpecFacet.material_card_id.in_(
                        db.query(MaterialSpecFacet.material_card_id).filter(
                            MaterialSpecFacet.category == commodity,
                            MaterialSpecFacet.spec_key == key,
                            MaterialSpecFacet.value_text.in_(values),
                        )
                    )
                )

    card_ids_subq = base_q.distinct().subquery()

    rows = (
        db.query(
            MaterialSpecFacet.spec_key,
            MaterialSpecFacet.value_text,
            func.count(MaterialSpecFacet.material_card_id.distinct()),
        )
        .filter(
            MaterialSpecFacet.category == commodity,
            MaterialSpecFacet.value_text.isnot(None),
            MaterialSpecFacet.material_card_id.in_(db.query(card_ids_subq)),
        )
        .group_by(MaterialSpecFacet.spec_key, MaterialSpecFacet.value_text)
        .all()
    )

    result: dict[str, dict[str, int]] = {}
    for spec_key, value, count in rows:
        result.setdefault(spec_key, {})[value] = count
    return result


def search_materials_faceted(
    db: Session,
    *,
    commodity: str | None = None,
    q: str | None = None,
    sub_filters: dict | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MaterialCard], int]:
    """Search materials with faceted filters.

    Args:
        commodity: Filter by commodity category (lowercased)
        q: Text search on MPN/manufacturer/description
        sub_filters: {spec_key: [values]} for enums, {spec_key_min: val} for ranges
        limit: Max results
        offset: Pagination offset

    Returns: (materials, total_count)
    """
    query = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))

    if commodity:
        query = query.filter(func.lower(func.trim(MaterialCard.category)) == commodity.lower().strip())

    if q:
        pattern = f"%{q}%"
        query = query.filter(
            (MaterialCard.normalized_mpn.ilike(pattern))
            | (MaterialCard.display_mpn.ilike(pattern))
            | (MaterialCard.manufacturer.ilike(pattern))
            | (MaterialCard.description.ilike(pattern))
        )

    if sub_filters and commodity:
        commodity_lower = commodity.lower().strip()
        for key, values in sub_filters.items():
            if key.endswith("_min"):
                spec_key = key[:-4]  # Remove _min suffix
                query = query.filter(
                    MaterialCard.id.in_(
                        db.query(MaterialSpecFacet.material_card_id).filter(
                            MaterialSpecFacet.category == commodity_lower,
                            MaterialSpecFacet.spec_key == spec_key,
                            MaterialSpecFacet.value_numeric >= values,
                        )
                    )
                )
            elif key.endswith("_max"):
                spec_key = key[:-4]
                query = query.filter(
                    MaterialCard.id.in_(
                        db.query(MaterialSpecFacet.material_card_id).filter(
                            MaterialSpecFacet.category == commodity_lower,
                            MaterialSpecFacet.spec_key == spec_key,
                            MaterialSpecFacet.value_numeric <= values,
                        )
                    )
                )
            elif isinstance(values, list) and values:
                query = query.filter(
                    MaterialCard.id.in_(
                        db.query(MaterialSpecFacet.material_card_id).filter(
                            MaterialSpecFacet.category == commodity_lower,
                            MaterialSpecFacet.spec_key == key,
                            MaterialSpecFacet.value_text.in_(values),
                        )
                    )
                )

    total = query.count()
    materials = (
        query.order_by(MaterialCard.search_count.desc(), MaterialCard.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return materials, total


def get_subfilter_options(db: Session, commodity: str) -> list[dict]:
    """Get sub-filter options for a commodity from schema + actual data.

    Returns list of dicts: {spec_key, display_name, data_type, values|range, unit, is_primary}
    """
    commodity = commodity.lower().strip()
    schemas = (
        db.query(CommoditySpecSchema)
        .filter_by(commodity=commodity, is_filterable=True)
        .order_by(CommoditySpecSchema.sort_order)
        .all()
    )

    result = []
    for schema in schemas:
        option = {
            "spec_key": schema.spec_key,
            "display_name": schema.display_name,
            "data_type": schema.data_type,
            "unit": schema.unit,
            "is_primary": schema.is_primary,
        }

        if schema.data_type == "enum":
            # Get actual values from data (not just enum_values from schema)
            actual = (
                db.query(MaterialSpecFacet.value_text)
                .filter(
                    MaterialSpecFacet.category == commodity,
                    MaterialSpecFacet.spec_key == schema.spec_key,
                    MaterialSpecFacet.value_text.isnot(None),
                )
                .distinct()
                .all()
            )
            option["values"] = sorted([r[0] for r in actual])

        elif schema.data_type == "numeric":
            # Get min/max from actual data
            agg = (
                db.query(
                    func.min(MaterialSpecFacet.value_numeric),
                    func.max(MaterialSpecFacet.value_numeric),
                )
                .filter(
                    MaterialSpecFacet.category == commodity,
                    MaterialSpecFacet.spec_key == schema.spec_key,
                    MaterialSpecFacet.value_numeric.isnot(None),
                )
                .first()
            )
            option["range"] = {"min": agg[0], "max": agg[1]} if agg and agg[0] is not None else None

        elif schema.data_type == "boolean":
            option["values"] = ["true", "false"]

        result.append(option)
    return result

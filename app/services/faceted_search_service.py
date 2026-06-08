"""Faceted search query service.

What: Builds faceted queries on material_cards + material_spec_facets.
      Provides commodity counts, facet counts, sub-filter options.
Called by: htmx_views.py faceted search routes
Depends on: MaterialCard, MaterialSpecFacet, CommoditySpecSchema
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.constants import MaterialEnrichmentStatus
from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.utils.search_builder import SearchBuilder

# Max distinct values rendered for an open-vocabulary (no enum_values) enum facet.
# Such facets get a typeahead search box + this many top-by-count values
# (see get_subfilter_options); a fixed-vocabulary facet renders its full canonical list.
TOP_N = 12


def _apply_facet_filters(
    query,
    db: Session,
    commodity: str,
    filters: dict,
    *,
    id_column=None,
) -> object:
    """Narrow *query* by facet filters (enum lists, numeric min/max).

    Args:
        id_column: The column to filter with ``.in_()``.
                   Defaults to ``MaterialSpecFacet.material_card_id``.
    """
    if id_column is None:
        id_column = MaterialSpecFacet.material_card_id

    for key, values in filters.items():
        if key.endswith("_min"):
            spec_key = key[:-4]
            query = query.filter(
                id_column.in_(
                    db.query(MaterialSpecFacet.material_card_id).filter(
                        MaterialSpecFacet.category == commodity,
                        MaterialSpecFacet.spec_key == spec_key,
                        MaterialSpecFacet.value_numeric >= values,
                    )
                )
            )
        elif key.endswith("_max"):
            spec_key = key[:-4]
            query = query.filter(
                id_column.in_(
                    db.query(MaterialSpecFacet.material_card_id).filter(
                        MaterialSpecFacet.category == commodity,
                        MaterialSpecFacet.spec_key == spec_key,
                        MaterialSpecFacet.value_numeric <= values,
                    )
                )
            )
        elif isinstance(values, list) and values:
            query = query.filter(
                id_column.in_(
                    db.query(MaterialSpecFacet.material_card_id).filter(
                        MaterialSpecFacet.category == commodity,
                        MaterialSpecFacet.spec_key == key,
                        MaterialSpecFacet.value_text.in_(values),
                    )
                )
            )
    return query


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
        base_q = _apply_facet_filters(base_q, db, commodity, active_filters)

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
            MaterialSpecFacet.material_card_id.in_(db.query(card_ids_subq.c.material_card_id)),
        )
        .group_by(MaterialSpecFacet.spec_key, MaterialSpecFacet.value_text)
        .all()
    )

    result: dict[str, dict[str, int]] = {}
    for spec_key, value, count in rows:
        result.setdefault(spec_key, {})[value] = count
    return result


def get_manufacturer_options(
    db: Session,
    commodity: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return distinct manufacturers sorted by card count (descending).

    Args:
        commodity: If set, scope to this commodity only.
        limit: Max results to return (default 20 per spec).

    Returns: [{"name": str, "count": int}, ...]
    """
    query = db.query(
        MaterialCard.manufacturer,
        func.count(MaterialCard.id).label("cnt"),
    ).filter(
        MaterialCard.deleted_at.is_(None),
        MaterialCard.manufacturer.isnot(None),
        MaterialCard.manufacturer != "",
    )

    if commodity:
        query = query.filter(func.lower(func.trim(MaterialCard.category)) == commodity.lower().strip())

    rows = query.group_by(MaterialCard.manufacturer).order_by(func.count(MaterialCard.id).desc()).limit(limit).all()
    return [{"name": name, "count": count} for name, count in rows]


def get_global_facet_counts(
    db: Session,
    commodity: str | None = None,
) -> dict[str, dict[str, int]]:
    """Return value counts for the global MaterialCard-column facets.

    These are columns that live directly on MaterialCard (not spec facets):
    ``lifecycle_status``, ``rohs_status`` and a derived ``has_datasheet`` boolean.

    Args:
        commodity: If set, scope counts to this commodity only.

    Returns: {"lifecycle": {value: count}, "rohs": {value: count},
              "has_datasheet": {"true": count}}
    """
    base = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))
    if commodity:
        base = base.filter(func.lower(func.trim(MaterialCard.category)) == commodity.lower().strip())

    def _count_col(column) -> dict[str, int]:
        rows = base.with_entities(column, func.count(MaterialCard.id)).filter(column.isnot(None)).group_by(column).all()
        return {val: count for val, count in rows if val}

    lifecycle_counts = _count_col(MaterialCard.lifecycle_status)
    rohs_counts = _count_col(MaterialCard.rohs_status)
    has_ds = base.with_entities(func.count(MaterialCard.id)).filter(MaterialCard.datasheet_url.isnot(None)).scalar()

    return {
        "lifecycle": lifecycle_counts,
        "rohs": rohs_counts,
        "has_datasheet": {"true": has_ds or 0},
    }


def search_materials_faceted(
    db: Session,
    *,
    commodity: str | None = None,
    q: str | None = None,
    sub_filters: dict | None = None,
    manufacturers: list[str] | None = None,
    verified_only: bool = False,
    statuses: list[str] | None = None,
    lifecycle: list[str] | None = None,
    rohs: list[str] | None = None,
    has_datasheet: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MaterialCard], int]:
    """Search materials with faceted filters.

    Args:
        commodity: Filter by commodity category (lowercased)
        q: Text search on MPN/manufacturer/description
        sub_filters: {spec_key: [values]} for enums, {spec_key_min: val} for ranges
        manufacturers: Restrict to these manufacturer names
        verified_only: Legacy boolean — when True (and ``statuses`` is empty), return only
            cards with enrichment_status == "verified"
        statuses: When provided, restrict to cards whose enrichment_status is in this list.
            Takes precedence over ``verified_only`` (the two are never ANDed).
        lifecycle: When provided, restrict to cards whose lifecycle_status is in this list
            (OR-within, e.g. ``["active", "eol"]``).
        rohs: When provided, restrict to cards whose rohs_status is in this list (OR-within).
        has_datasheet: When True, restrict to cards that have a non-null datasheet_url.
        limit: Max results
        offset: Pagination offset

    Returns: (materials, total_count)
    """
    query = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None))

    if commodity:
        query = query.filter(func.lower(func.trim(MaterialCard.category)) == commodity.lower().strip())

    _fts_applied = False
    if q:
        sb = SearchBuilder(q)
        is_pg = db.get_bind().dialect.name == "postgresql"
        # Short/single-token queries (likely MPN prefixes) → ILIKE for substring match
        # Multi-word natural language queries → FTS for relevance ranking
        use_fts = is_pg and " " in q.strip()
        if use_fts:
            from sqlalchemy import or_

            ts_query = func.plainto_tsquery("english", q)
            # Combine FTS with ILIKE on MPN fields (FTS misses partial MPN matches)
            query = query.filter(
                or_(
                    MaterialCard.search_vector.op("@@")(ts_query),
                    MaterialCard.display_mpn.ilike(f"%{sb.safe}%"),
                    MaterialCard.normalized_mpn.ilike(f"%{sb.safe}%"),
                )
            )
            query = query.order_by(
                func.ts_rank(MaterialCard.search_vector, ts_query).desc(),
                MaterialCard.search_count.desc(),
            )
            _fts_applied = True
        else:
            # Single-token or SQLite: substring match on all fields
            query = query.filter(
                sb.ilike_filter(
                    MaterialCard.normalized_mpn,
                    MaterialCard.display_mpn,
                    MaterialCard.manufacturer,
                    MaterialCard.description,
                )
            )

    if manufacturers:
        query = query.filter(MaterialCard.manufacturer.in_(manufacturers))

    # `statuses` (multi-select) takes precedence over the legacy `verified_only` boolean.
    # ANDing both would yield an impossible filter (e.g. status==verified AND status IN
    # ('web_sourced')) and silently return nothing.
    if statuses:
        query = query.filter(MaterialCard.enrichment_status.in_(statuses))
    elif verified_only:
        query = query.filter(MaterialCard.enrichment_status == MaterialEnrichmentStatus.VERIFIED)

    # Global facets — clean MaterialCard columns (OR-within each facet).
    if lifecycle:
        query = query.filter(MaterialCard.lifecycle_status.in_(lifecycle))
    if rohs:
        query = query.filter(MaterialCard.rohs_status.in_(rohs))
    if has_datasheet:
        query = query.filter(MaterialCard.datasheet_url.isnot(None))

    if sub_filters and commodity:
        commodity_lower = commodity.lower().strip()
        query = _apply_facet_filters(
            query,
            db,
            commodity_lower,
            sub_filters,
            id_column=MaterialCard.id,
        )

    total = db.query(func.count()).select_from(query.subquery()).scalar()

    if not _fts_applied:
        query = query.order_by(MaterialCard.search_count.desc(), MaterialCard.created_at.desc())

    materials = query.offset(offset).limit(limit).all()
    return materials, total


def get_subfilter_options(db: Session, commodity: str) -> list[dict]:
    """Get sub-filter options for a commodity from schema + actual data.

    Uses 3 queries total (schema + text values + numeric ranges) instead of
    1 + N queries per schema row, avoiding N+1.

    Returns list of dicts: {spec_key, display_name, data_type, values|range, unit, is_primary}
    """
    commodity = commodity.lower().strip()
    schemas = (
        db.query(CommoditySpecSchema)
        .filter_by(commodity=commodity, is_filterable=True)
        .order_by(CommoditySpecSchema.sort_order)
        .all()
    )
    if not schemas:
        return []

    # Batch query: observed text values + their counts, grouped by spec_key.
    # text_map = sorted observed values (used to append unexpected values to a fixed vocab);
    # count_map = {spec_key: {value: count}} (drives open-vocab top-N selection).
    text_count_rows = (
        db.query(
            MaterialSpecFacet.spec_key,
            MaterialSpecFacet.value_text,
            func.count(MaterialSpecFacet.material_card_id.distinct()),
        )
        .filter(
            MaterialSpecFacet.category == commodity,
            MaterialSpecFacet.value_text.isnot(None),
        )
        .group_by(MaterialSpecFacet.spec_key, MaterialSpecFacet.value_text)
        .all()
    )
    text_map: dict[str, list[str]] = {}
    count_map: dict[str, dict[str, int]] = {}
    for sk, vt, cnt in text_count_rows:
        text_map.setdefault(sk, []).append(vt)
        count_map.setdefault(sk, {})[vt] = cnt
    for k in text_map:
        text_map[k].sort()

    # Batch query: min/max for numeric specs
    numeric_rows = (
        db.query(
            MaterialSpecFacet.spec_key,
            func.min(MaterialSpecFacet.value_numeric),
            func.max(MaterialSpecFacet.value_numeric),
        )
        .filter(
            MaterialSpecFacet.category == commodity,
            MaterialSpecFacet.value_numeric.isnot(None),
        )
        .group_by(MaterialSpecFacet.spec_key)
        .all()
    )
    numeric_map: dict[str, dict] = {}
    for sk, mn, mx in numeric_rows:
        numeric_map[sk] = {"min": mn, "max": mx}

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
            if schema.enum_values:
                # Fixed vocabulary: render the full canonical list (so unstocked values
                # still show with a (0) count), then append any unexpected observed values.
                observed = set(text_map.get(schema.spec_key, []))
                option["values"] = list(schema.enum_values) + [
                    v for v in sorted(observed) if v not in schema.enum_values
                ]
                option["widget"] = "checkbox"
            else:
                # Open vocabulary (e.g. connector series): no canonical list to enumerate,
                # so offer the top-N observed values by count + a typeahead search box.
                observed_counts = count_map.get(schema.spec_key, {})
                option["values"] = sorted(observed_counts, key=lambda v: observed_counts[v], reverse=True)[:TOP_N]
                option["widget"] = "typeahead"
                option["total_distinct"] = len(observed_counts)
        elif schema.data_type == "numeric":
            option["range"] = numeric_map.get(schema.spec_key)
            option["widget"] = "range"
        elif schema.data_type == "boolean":
            # Always offer Yes/No (with counts incl. 0) so the toggle renders consistently
            # regardless of whether data currently backs it.
            option["values"] = ["true", "false"]
        result.append(option)
    return result

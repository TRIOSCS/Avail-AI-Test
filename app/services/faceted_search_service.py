"""Faceted search query service.

What: Builds faceted queries on material_cards + material_spec_facets.
      Provides commodity counts, facet counts, sub-filter options.
Called by: htmx_views.py faceted search routes
Depends on: MaterialCard, MaterialSpecFacet, CommoditySpecSchema
"""

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import Text, cast, exists, func, or_
from sqlalchemy.orm import Session

from app.constants import MaterialEnrichmentStatus
from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet, MaterialVendorHistory
from app.utils.search_builder import SearchBuilder

# Max distinct values rendered for an open-vocabulary (no enum_values) enum facet.
# Such facets get a typeahead search box + this many top-by-count values
# (see get_subfilter_options); a fixed-vocabulary facet renders its full canonical list.
TOP_N = 12

# Operational (Layer-3) filter vocabularies. This service OWNS the vocabularies: the
# maps below drive the query branches in search_materials_faceted, and the *_VALUES
# tuples (sentinel + map keys) are derived from them — adding a mode/bucket to a map
# wires the query branch and the route check together. The faceted ROUTE
# (htmx_views.materials_faceted_partial) validates incoming params against the *_VALUES
# tuples and degrades unknowns to the no-op sentinel with a WARNING log; inside this
# service unknown values simply fall through the map lookups as silent no-ops.
# Front-end twin (must stay in sync): INTERNAL_MODES / SEARCH_BUCKETS on the
# materialsFilter Alpine component in app/static/htmx_app.js.
_INTERNAL_MODE_PREDICATES = {
    # mode -> zero-arg predicate factory on MaterialCard.is_internal_part.
    "standard": lambda: or_(MaterialCard.is_internal_part.is_(False), MaterialCard.is_internal_part.is_(None)),
    "internal": lambda: MaterialCard.is_internal_part.is_(True),
}
INTERNAL_FILTER_VALUES = ("all", *_INTERNAL_MODE_PREDICATES)  # "all" = no-op sentinel
SEARCHED_WITHIN_DAYS = {"7d": 7, "30d": 30, "90d": 90}
SEARCHED_WITHIN_VALUES = ("any", *SEARCHED_WITHIN_DAYS)  # "any" = no-op sentinel


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
    """Return {commodity_key: count} for all non-deleted material cards.

    Filters and counts ONLY on the lower(trim(category)) expression (not the raw
    column) with count(*) so PostgreSQL can answer the whole GROUP BY from an
    index-only scan over ix_mc_cat_order_live (098_materials_perf_idx) — referencing
    the raw ``category`` column or ``count(id)`` would force heap fetches. Equivalent
    semantics: lower(trim(x)) IS NOT NULL iff x IS NOT NULL (both functions are
    strict), and id is the non-null PK so count(id) == count(*).
    """
    cat_expr = func.lower(func.trim(MaterialCard.category))
    rows = (
        db.query(cat_expr, func.count())
        .filter(MaterialCard.deleted_at.is_(None), cat_expr.isnot(None))
        .group_by(cat_expr)
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
    active_filters = active_filters or {}

    def _grouped_counts(narrow_filters: dict, only_spec_key: str | None = None) -> list:
        base = db.query(MaterialSpecFacet.material_card_id).filter(MaterialSpecFacet.category == commodity)
        if narrow_filters:
            base = _apply_facet_filters(base, db, commodity, narrow_filters)
        card_ids_subq = base.distinct().subquery()
        q = db.query(
            MaterialSpecFacet.spec_key,
            MaterialSpecFacet.value_text,
            func.count(MaterialSpecFacet.material_card_id.distinct()),
        ).filter(
            MaterialSpecFacet.category == commodity,
            MaterialSpecFacet.value_text.isnot(None),
            MaterialSpecFacet.material_card_id.in_(db.query(card_ids_subq.c.material_card_id)),
        )
        if only_spec_key is not None:
            q = q.filter(MaterialSpecFacet.spec_key == only_spec_key)
        return q.group_by(MaterialSpecFacet.spec_key, MaterialSpecFacet.value_text).all()

    # Pass 1: every facet narrowed by ALL active filters — correct for facets the user has NOT
    # filtered on (they should reflect the full current narrowing).
    result: dict[str, dict[str, int]] = {}
    for spec_key, value, count in _grouped_counts(active_filters):
        result.setdefault(spec_key, {})[value] = count

    # Pass 2: OR-within-facet correctness — recompute each ACTIVELY-FILTERED enum facet's own
    # value counts against the set narrowed by every OTHER facet (excluding its own selection),
    # so checking one value never collapses its siblings to 0. (Enum filters carry list values;
    # numeric _min/_max filters have no value_text counts, so they are skipped.)
    enum_filtered_keys = [k for k, v in active_filters.items() if isinstance(v, list)]
    for fk in enum_filtered_keys:
        others = {k: v for k, v in active_filters.items() if k not in (fk, f"{fk}_min", f"{fk}_max")}
        result[fk] = {value: count for _sk, value, count in _grouped_counts(others, only_spec_key=fk)}
    return result


def get_manufacturer_options(
    db: Session,
    commodity: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return distinct brand/maker names sorted by card count (descending).

    Dual-brand (migration 097): the "Brand" facet is ONE combined facet over BOTH
    columns — ``brand`` (OEM label: IBM, Dell Technologies) and ``manufacturer``
    (actual maker: Seagate Technology). UNION ALL over the two columns, deduped by
    card (a card with ``brand == manufacturer`` counts once via COUNT(DISTINCT id)).

    Args:
        commodity: If set, scope to this commodity only (applied inside BOTH branches).
        limit: Max results to return (default 20 per spec).

    Returns: [{"name": str, "count": int}, ...]
    """

    def _branch(column):
        q = db.query(MaterialCard.id.label("card_id"), column.label("name")).filter(
            MaterialCard.deleted_at.is_(None),
            column.isnot(None),
            column != "",
        )
        if commodity:
            q = q.filter(func.lower(func.trim(MaterialCard.category)) == commodity.lower().strip())
        return q

    union = _branch(MaterialCard.brand).union_all(_branch(MaterialCard.manufacturer)).subquery()
    cnt = func.count(func.distinct(union.c.card_id))
    rows = db.query(union.c.name, cnt.label("cnt")).group_by(union.c.name).order_by(cnt.desc()).limit(limit).all()
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
    condition_counts = _count_col(MaterialCard.condition)
    has_ds = base.with_entities(func.count(MaterialCard.id)).filter(MaterialCard.datasheet_url.isnot(None)).scalar()

    return {
        "lifecycle": lifecycle_counts,
        "rohs": rohs_counts,
        "condition": condition_counts,
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
    condition: list[str] | None = None,
    has_datasheet: bool = False,
    has_stock: bool = False,
    has_price: bool = False,
    has_crosses: bool = False,
    internal: str = "all",
    searched_within: str = "any",
    min_searches: int = 0,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[MaterialCard], int]:
    """Search materials with faceted filters.

    Args:
        commodity: Filter by commodity category (lowercased)
        q: Text search on MPN/manufacturer/description
        sub_filters: {spec_key: [values]} for enums, {spec_key_min: val} for ranges
        manufacturers: Restrict to cards whose manufacturer OR brand is in this list
            (the combined dual-brand facet — OR-within, AND-across-facets; the wire
            param keeps its legacy "manufacturers" name for back-compat)
        verified_only: Legacy boolean — when True (and ``statuses`` is empty), return only
            cards with enrichment_status == "verified"
        statuses: When provided, restrict to cards whose enrichment_status is in this list.
            Takes precedence over ``verified_only`` (the two are never ANDed).
        lifecycle: When provided, restrict to cards whose lifecycle_status is in this list
            (OR-within, e.g. ``["active", "eol"]``).
        rohs: When provided, restrict to cards whose rohs_status is in this list (OR-within).
        has_datasheet: When True, restrict to cards that have a non-null datasheet_url.
        has_stock: When True, restrict to cards with at least one vendor-history row
            ("has vendor sightings / stock seen").
        has_price: When True, restrict to cards with a vendor-history row carrying a
            recorded last_price.
        has_crosses: When True, restrict to cards whose cross_references JSON holds a
            non-empty list (portable across PostgreSQL JSONB and SQLite JSON-as-text).
        internal: Tri-state — "all" (no-op), "standard" (is_internal_part FALSE/NULL),
            "internal" (is_internal_part TRUE). Unknown values degrade to "all".
        searched_within: Recency bucket on last_searched_at — "7d" | "30d" | "90d" |
            "any" (no-op). Unknown values degrade to "any".
        min_searches: Minimum search_count (0 = no-op).
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
        # Dual-brand: the combined "Brand" facet ORs across both columns — a buyer
        # filtering "IBM" matches an IBM-labeled drive made by Seagate (brand=IBM) AND
        # filtering "Seagate Technology" matches the same card (manufacturer). Strict
        # superset of the old single-column match, so old bookmarks keep working.
        query = query.filter(
            or_(
                MaterialCard.manufacturer.in_(manufacturers),
                MaterialCard.brand.in_(manufacturers),
            )
        )

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
    if condition:
        query = query.filter(MaterialCard.condition.in_(condition))
    if has_datasheet:
        query = query.filter(MaterialCard.datasheet_url.isnot(None))

    # Operational (Layer-3) sourcing filters — MaterialCard columns + vendor history.
    if has_stock:
        query = query.filter(exists().where(MaterialVendorHistory.material_card_id == MaterialCard.id))
    if has_price:
        query = query.filter(
            exists().where(
                MaterialVendorHistory.material_card_id == MaterialCard.id,
                MaterialVendorHistory.last_price.isnot(None),
            )
        )
    if has_crosses:
        # Portable non-empty-JSON-list predicate: PG jsonb::text renders an empty array
        # as '[]' and a JSON null as 'null'; SQLite stores json.dumps() output, which
        # matches the same literals. Avoids PG-only jsonb_array_length() that SQLite
        # tests would silently mis-handle.
        query = query.filter(
            MaterialCard.cross_references.isnot(None),
            cast(MaterialCard.cross_references, Text).notin_(("[]", "null", "")),
        )
    internal_predicate = _INTERNAL_MODE_PREDICATES.get(internal)
    if internal_predicate is not None:
        query = query.filter(internal_predicate())
    days = SEARCHED_WITHIN_DAYS.get(searched_within)
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.filter(MaterialCard.last_searched_at >= cutoff)
    if min_searches and min_searches > 0:
        query = query.filter(MaterialCard.search_count >= min_searches)

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
                # Open vocabulary (e.g. motherboard chipset): no canonical list to enumerate,
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


class SpecCoverage(NamedTuple):
    """Parametric-spec coverage for a commodity (invariant: 0 <= with_specs <=
    total)."""

    with_specs: int
    total: int


def get_commodity_spec_coverage(db: Session, commodity: str) -> SpecCoverage:
    """Return parametric-spec coverage for a commodity.

    SpecCoverage(with_specs=N, total=M) — N = distinct non-deleted cards in the
    commodity that have at least one MaterialSpecFacet row, M = all non-deleted cards in
    the commodity. Drives the coverage line in the sub-filters panel and the coverage-
    aware empty state (a parametric zero-result mostly means "not yet spec-enriched",
    not "no such parts"). Two cheap aggregates, no N+1.
    """
    commodity = commodity.lower().strip()
    commodity_cards = db.query(MaterialCard.id).filter(
        MaterialCard.deleted_at.is_(None),
        func.lower(func.trim(MaterialCard.category)) == commodity,
    )
    total = db.query(func.count()).select_from(commodity_cards.subquery()).scalar() or 0
    with_specs = (
        db.query(func.count(func.distinct(MaterialSpecFacet.material_card_id)))
        .filter(
            MaterialSpecFacet.category == commodity,
            MaterialSpecFacet.material_card_id.in_(commodity_cards),
        )
        .scalar()
        or 0
    )
    return SpecCoverage(with_specs=with_specs, total=total)

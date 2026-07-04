"""routers/htmx/materials.py — Materials partial views (HTMX + Alpine).

Server-rendered HTML partials for the materials surface: the faceted list + filter
sidebars (manufacturers/global/tree/sub), manufacturer search/add, AI interpret,
faceted results, add-form, enrich-status poller, conflict-accept, FRU lookup, the
material detail panel + tabs, card update, and the enrich/find-crosses/insights
actions. Holds the shared faceted-filter param parsers. Extracted verbatim from
htmx_views.py (same `/v2/partials/materials` + `/v2/partials/manufacturers` paths,
same `htmx-views` tag). NB: distinct from the domain router app/routers/materials.py.

Called by: app/main.py (router mount).
Depends on: app.models, app.dependencies, app.database, app.services, ._shared
"""

import html
import json

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...constants import (
    AccessKey,
)
from ...database import get_db
from ...dependencies import (
    has_buyer_role,
    require_access,
    require_buyer,
)
from ...models import (
    Offer,
    User,
)
from ...models.faceted_search import CommoditySpecSchema
from ...services.commodity_registry import COMMODITY_TREE, get_display_name
from ...services.faceted_search_service import (
    INTERNAL_FILTER_VALUES,
    SEARCHED_WITHIN_VALUES,
    get_commodity_counts,
    get_commodity_spec_coverage,
    get_facet_counts,
    get_global_facet_counts,
    get_subfilter_options,
    search_materials_faceted,
)
from ...services.part_history_service import (
    customer_purchases_for_card,
    offers_for_card,
    requirements_for_card,
    sightings_for_card,
)
from ...template_env import template_response
from ...utils.sql_helpers import escape_like
from ._shared import _base_ctx

router = APIRouter(tags=["htmx-views"])


def _parse_filter_json(raw: str, *, coerce_numeric: bool = False) -> dict:
    """Parse a JSON filter string into a dict, returning {} on failure.

    When coerce_numeric=True, keys ending in _min/_max are cast to float.
    """
    try:
        parsed: dict = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    if not coerce_numeric:
        return parsed
    result: dict = {}
    for key, val in parsed.items():
        if key.endswith("_min") or key.endswith("_max"):
            try:
                result[key] = float(val)
            except (ValueError, TypeError):
                pass
        else:
            result[key] = val
    return result


def _pop_manufacturers(parsed_filters: dict) -> list[str] | None:
    """Pop the 'manufacturers' key out of a parsed sub_filters dict.

    'manufacturers' is a MaterialCard column (the combined dual-brand facet), not a spec
    facet — left in the dict it would zero every spec-facet count. Shared by the faceted
    results route and both sidebar count routes.
    """
    if not parsed_filters:
        return None
    mfr_val = parsed_filters.pop("manufacturers", None)
    if not mfr_val:
        return None
    return mfr_val if isinstance(mfr_val, list) else [mfr_val]


def _parse_card_filter_params(
    statuses: str,
    lifecycle: str,
    rohs: str,
    condition: str,
    has_datasheet: str,
    has_validation_conflict: str,
    has_stock: str,
    has_price: str,
    has_crosses: str,
    internal: str,
    searched_within: str,
    min_searches: str,
) -> dict:
    """Parse the card-level faceted filter params shared by the results-list route and
    BOTH sidebar count routes (sub-filters + global), so the list and the counts can
    never read the same query string differently.

    Unknown/invalid values (incl. non-numeric/negative min_searches and the boolean
    flags) degrade to the no-op default — hand-edited URLs must not 500/422 (a 422
    partial never swaps, htmx shows only the generic error toast) — but each degrade is
    LOGGED so frontend/backend vocabulary drift (e.g. a bucket added to the UI but not
    the backend constants) surfaces in logs instead of silently no-op'ing the filter
    while the active-filter chip claims it is applied.

    Returns keyword args for faceted_search_service (minus commodity / q / sub_filters /
    manufacturers, which each route binds itself).
    """

    def _csv_list(raw: str) -> list[str] | None:
        items = [s.strip() for s in raw.split(",") if s.strip()]
        return items or None

    def _flag(name: str, raw: str) -> bool:
        val = raw.strip().lower()
        if val in {"true", "1", "yes", "on"}:
            return True
        if val not in {"false", "0", "", "no", "off"}:
            logger.warning("materials faceted: invalid {}={!r}, degrading to false", name, raw)
        return False

    def _choice(name: str, raw: str, valid: tuple[str, ...], default: str) -> str:
        if raw in valid:
            return raw
        logger.warning("materials faceted: unknown {}={!r}, degrading to {!r}", name, raw, default)
        return default

    try:
        min_searches_n = int(min_searches)
    except ValueError:
        min_searches_n = -1
    if min_searches_n < 0:
        logger.warning("materials faceted: invalid min_searches={!r}, degrading to 0", min_searches)
        min_searches_n = 0

    return {
        "statuses": _csv_list(statuses),
        "lifecycle": _csv_list(lifecycle),
        "rohs": _csv_list(rohs),
        "condition": _csv_list(condition),
        "has_datasheet": _flag("has_datasheet", has_datasheet),
        "has_validation_conflict": _flag("has_validation_conflict", has_validation_conflict),
        "has_stock": _flag("has_stock", has_stock),
        "has_price": _flag("has_price", has_price),
        "has_crosses": _flag("has_crosses", has_crosses),
        "internal": _choice("internal", internal, INTERNAL_FILTER_VALUES, "all"),
        "searched_within": _choice("searched_within", searched_within, SEARCHED_WITHIN_VALUES, "any"),
        "min_searches": min_searches_n,
    }


# ── Materials partials ────────────────────────────────────────────────


@router.get("/v2/partials/materials", response_class=HTMLResponse)
async def materials_list_partial(
    request: Request,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Redirect to faceted workspace — all materials browsing uses the sidebar layout.

    Gated by MATERIALS access (it calls workspace_partial directly, so the inner route's
    Depends would otherwise never run).
    """
    return await materials_workspace_partial(request, user, db)


@router.get("/v2/partials/materials/workspace", response_class=HTMLResponse)
async def materials_workspace_partial(
    request: Request,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Render the faceted search workspace layout."""
    from ...models.intelligence import MaterialCard

    total_materials = db.query(MaterialCard).filter(MaterialCard.deleted_at.is_(None)).count()
    all_subs = [sub for subs in COMMODITY_TREE.values() for sub in subs]
    ctx = _base_ctx(request, user, "materials")
    ctx["total_materials"] = total_materials
    ctx["display_names"] = {sub: get_display_name(sub) for sub in all_subs}
    ctx["global_facet_counts"] = get_global_facet_counts(db)
    # The workspace is MATERIALS-gated, but POST /api/materials/add is require_buyer —
    # hide the "Add part" button from roles whose submit would 403 (dead-end otherwise).
    ctx["can_add_parts"] = has_buyer_role(user)
    return template_response("htmx/partials/materials/workspace.html", ctx)


@router.get("/v2/partials/materials/filters/manufacturers", response_class=HTMLResponse)
async def materials_filters_manufacturers_partial(
    request: Request,
    commodity: str = "",
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Render manufacturer filter dropdown."""
    from ...services.faceted_search_service import get_manufacturer_options

    options = get_manufacturer_options(db, commodity=commodity or None)
    ctx = _base_ctx(request, user, "materials")
    ctx["manufacturer_options"] = options
    return template_response("htmx/partials/materials/filters/manufacturers.html", ctx)


@router.get("/v2/partials/materials/filters/global", response_class=HTMLResponse)
async def materials_filters_global_partial(
    request: Request,
    commodity: str = "",
    q: str = "",
    sub_filters: str = "{}",
    statuses: str = "",
    lifecycle: str = "",
    rohs: str = "",
    condition: str = "",
    has_datasheet: str = "false",
    has_validation_conflict: str = "false",
    has_stock: str = "false",
    has_price: str = "false",
    has_crosses: str = "false",
    internal: str = "all",
    searched_within: str = "any",
    min_searches: str = "0",
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Render global facets (lifecycle / RoHS / condition / has-datasheet) with live
    counts.

    Receives the FULL active filter set (same wire params as the results list) so the
    rendered counts match the visible results instead of overstating; each facet's own
    selection is excluded inside get_global_facet_counts (self-exclusion).
    """
    parsed_filters = _parse_filter_json(sub_filters, coerce_numeric=True)
    filters = _parse_card_filter_params(
        statuses,
        lifecycle,
        rohs,
        condition,
        has_datasheet,
        has_validation_conflict,
        has_stock,
        has_price,
        has_crosses,
        internal,
        searched_within,
        min_searches,
    )
    filters["manufacturers"] = _pop_manufacturers(parsed_filters)
    filters["q"] = q or None
    filters["sub_filters"] = parsed_filters or None
    counts = get_global_facet_counts(db, commodity=commodity or None, filters=filters)
    ctx = _base_ctx(request, user, "materials")
    ctx["global_facet_counts"] = counts
    return template_response("htmx/partials/materials/filters/global.html", ctx)


@router.get("/v2/partials/manufacturers/search", response_class=HTMLResponse)
async def manufacturer_search(
    request: Request,
    q: str = "",
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Typeahead search for manufacturers by name or alias."""
    from sqlalchemy import Text, cast

    from ...models.sourcing import Manufacturer

    results = []
    if q.strip():
        pattern = f"%{escape_like(q.strip())}%"
        by_name = db.query(Manufacturer).filter(Manufacturer.canonical_name.ilike(pattern, escape="\\")).limit(10).all()
        results = list(by_name)
        if len(results) < 10:
            seen_ids = {r.id for r in results}
            alias_matches = (
                db.query(Manufacturer)
                .filter(
                    Manufacturer.id.notin_(seen_ids),
                    cast(Manufacturer.aliases, Text).ilike(pattern, escape="\\"),
                )
                .limit(10 - len(results))
                .all()
            )
            results.extend(alias_matches)

    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"results": results, "q": q.strip()})
    return template_response("htmx/partials/manufacturers/search_results.html", ctx)


@router.post("/v2/partials/manufacturers/add", response_class=HTMLResponse)
async def manufacturer_add(
    request: Request,
    name: str = Form(...),
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Add a new manufacturer on the fly from typeahead."""
    from ...models.sourcing import Manufacturer

    name = name.strip()
    if not name:
        return HTMLResponse('<div class="px-3 py-1.5 text-xs text-red-500">Name required</div>')

    existing = db.query(Manufacturer).filter_by(canonical_name=name).first()
    if not existing:
        mfr = Manufacturer(canonical_name=name)
        db.add(mfr)
        db.commit()

    return HTMLResponse(
        f'<div class="px-3 py-1.5 text-xs font-medium text-brand-600" data-mfr-name="{html.escape(name)}">Added: {html.escape(name)}</div>'
    )


@router.get("/v2/partials/materials/filters/tree", response_class=HTMLResponse)
async def materials_filters_tree_partial(
    request: Request,
    commodity: str = "",
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Render the commodity category tree for the faceted sidebar."""
    commodity_counts = get_commodity_counts(db)
    # Build display_names dict for template (.get() usage)
    all_subs: list[str] = [sub for subs in COMMODITY_TREE.values() for sub in subs]
    display_names = {sub: get_display_name(sub) for sub in all_subs}
    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "commodity_tree": COMMODITY_TREE,
            "commodity_counts": commodity_counts,
            "display_names": display_names,
            "active_commodity": commodity.lower().strip() if commodity else "",
        }
    )
    return template_response("htmx/partials/materials/filters/tree.html", ctx)


@router.get("/v2/partials/materials/filters/sub", response_class=HTMLResponse)
async def materials_filters_sub_partial(
    request: Request,
    commodity: str = "",
    sub_filters: str = "{}",
    q: str = "",
    statuses: str = "",
    lifecycle: str = "",
    rohs: str = "",
    condition: str = "",
    has_datasheet: str = "false",
    has_validation_conflict: str = "false",
    has_stock: str = "false",
    has_price: str = "false",
    has_crosses: str = "false",
    internal: str = "all",
    searched_within: str = "any",
    min_searches: str = "0",
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Render sub-filters for a selected commodity with live facet counts.

    Receives the FULL active filter set (same wire params as the results list) so facet
    counts reflect active q / brand / confidence / global / sourcing filters instead of
    overstating; spec-filter self-exclusion (OR-within-facet) stays inside
    get_facet_counts pass 2.
    """
    if not commodity.strip():
        # No commodity scope — render the placeholder nudge (skip the facet/coverage
        # service calls; subfilters.html handles the commodity_selected=False branch).
        ctx = _base_ctx(request, user, "materials")
        ctx["commodity_selected"] = False
        return template_response("htmx/partials/materials/filters/subfilters.html", ctx)

    # Parse active filters so facet counts reflect current selection.
    parsed_filters = _parse_filter_json(sub_filters)
    # Card-level narrowing — shared wire-param parsing with the results list, plus the
    # 'manufacturers' entry that rides inside sub_filters (a MaterialCard column, not a
    # spec facet — left in parsed_filters it would zero every facet count).
    card_filters = _parse_card_filter_params(
        statuses,
        lifecycle,
        rohs,
        condition,
        has_datasheet,
        has_validation_conflict,
        has_stock,
        has_price,
        has_crosses,
        internal,
        searched_within,
        min_searches,
    )
    card_filters["manufacturers"] = _pop_manufacturers(parsed_filters)
    card_filters["q"] = q or None

    subfilter_options = get_subfilter_options(db, commodity)
    facet_counts = get_facet_counts(db, commodity, active_filters=parsed_filters or None, card_filters=card_filters)
    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "subfilter_options": subfilter_options,
            "facet_counts": facet_counts,
            "commodity_selected": True,
            "spec_coverage": get_commodity_spec_coverage(db, commodity),
            "commodity_display": get_display_name(commodity),
        }
    )
    return template_response("htmx/partials/materials/filters/subfilters.html", ctx)


@router.get("/v2/partials/materials/ai-interpret", response_class=HTMLResponse)
async def materials_ai_interpret_partial(
    request: Request,
    q: str = "",
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Interpret a natural language query using AI and return pre-selection chip."""
    from ...services.materials_ai_search import get_parent_for_commodity, interpret_search_query

    result = None
    if q and len(q.strip().split()) >= 3:
        result = await interpret_search_query(q)

    ctx = _base_ctx(request, user, "materials")
    ctx["ai_result"] = result
    if result and result.get("commodity"):
        ctx["ai_parent"] = get_parent_for_commodity(result["commodity"])
    else:
        ctx["ai_parent"] = ""
    return template_response("htmx/partials/materials/ai_interpret.html", ctx)


@router.get("/v2/partials/materials/faceted", response_class=HTMLResponse)
async def materials_faceted_partial(
    request: Request,
    commodity: str = "",
    q: str = "",
    sub_filters: str = "{}",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    verified_only: bool = Query(False),
    statuses: str = Query(""),
    lifecycle: str = Query(""),
    rohs: str = Query(""),
    condition: str = Query(""),
    has_datasheet: str = Query("false"),
    has_validation_conflict: str = Query("false"),
    has_stock: str = Query("false"),
    has_price: str = Query("false"),
    has_crosses: str = Query("false"),
    internal: str = Query("all"),
    searched_within: str = Query("any"),
    min_searches: str = Query("0"),
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Return faceted-search material list as HTML partial."""
    from ...models.intelligence import MaterialVendorHistory

    parsed_filters = _parse_filter_json(sub_filters, coerce_numeric=True)
    manufacturers = _pop_manufacturers(parsed_filters)

    # Shared degrade-don't-500 parsing — same helper as both sidebar count routes, so
    # the list and the counts can never read the same query string differently.
    card_params = _parse_card_filter_params(
        statuses,
        lifecycle,
        rohs,
        condition,
        has_datasheet,
        has_validation_conflict,
        has_stock,
        has_price,
        has_crosses,
        internal,
        searched_within,
        min_searches,
    )

    materials, total = search_materials_faceted(
        db,
        commodity=commodity or None,
        q=q or None,
        sub_filters=parsed_filters or None,
        manufacturers=manufacturers,
        verified_only=verified_only,
        **card_params,
        limit=limit,
        offset=offset,
    )

    # Attach vendor stats (matching existing materials list pattern)
    card_ids = [m.id for m in materials]
    vendor_stats: dict = {}
    if card_ids:
        stats = (
            db.query(
                MaterialVendorHistory.material_card_id,
                sqlfunc.count(MaterialVendorHistory.id),
                sqlfunc.min(MaterialVendorHistory.last_price),
                sqlfunc.count(sqlfunc.distinct(MaterialVendorHistory.last_currency)),
                sqlfunc.max(MaterialVendorHistory.last_currency),
            )
            .filter(MaterialVendorHistory.material_card_id.in_(card_ids))
            .group_by(MaterialVendorHistory.material_card_id)
            .all()
        )
        # currency shown only when a card's vendor rows are single-currency; mixed → default $
        vendor_stats = {s[0]: (s[1], s[2], s[4] if s[3] == 1 else None) for s in stats}

    # Attach spec chips for display. In commodity context: the selected commodity's
    # is_primary keys (same keys as before; non-scalar/missing values are now SKIPPED
    # instead of rendering dict-reprs or 500ing on raw-scalar entries). Without a
    # commodity: each card's OWN category's primary keys (one batched query — no N+1);
    # whenever that yields no chips (schema-less category OR a card lacking values for
    # every primary key) fall back to the first 3 scalar specs_structured entries; the
    # template renders "label: value" there.
    def _spec_scalar(raw):
        val = raw.get("value") if isinstance(raw, dict) else raw
        return val if isinstance(val, (str, int, float, bool)) else None

    primary_by_cat: dict[str, dict[str, str]] = {}
    if commodity:
        primary_by_cat[commodity.lower().strip()] = {
            s.spec_key: s.display_name
            for s in db.query(CommoditySpecSchema).filter_by(commodity=commodity, is_primary=True).all()
        }
    else:
        card_cats = {(m.category or "").lower().strip() for m in materials if m.category}
        if card_cats:
            schema_rows = (
                db.query(CommoditySpecSchema)
                .filter(CommoditySpecSchema.commodity.in_(card_cats), CommoditySpecSchema.is_primary.is_(True))
                .all()
            )
            for s in schema_rows:
                primary_by_cat.setdefault(s.commodity, {})[s.spec_key] = s.display_name

    # Dual-brand cell: the " · maker" suffix renders only when brand (OEM label) and
    # manufacturer (actual maker) are DIFFERENT COMPANIES. Compare NORMALIZED forms, not
    # raw strings — B1 writes the canonical OEM into brand while manufacturer keeps the
    # raw alias (lossless by design), so an exact-string compare renders tautologies like
    # "Hewlett Packard Enterprise · HP" (the same company twice).
    from ...services.manufacturer_normalizer import normalize_brand_name

    for m in materials:
        vc, bp, cur = vendor_stats.get(m.id, (0, None, None))
        m._vendor_count = vc
        m._best_price = bp
        m._best_currency = cur
        m._show_maker_suffix = bool(
            m.brand
            and m.manufacturer
            and normalize_brand_name(db, m.brand).lower() != normalize_brand_name(db, m.manufacturer).lower()
        )
        specs = m.specs_structured or {}
        card_cat = commodity.lower().strip() if commodity else (m.category or "").lower().strip()
        primary_keys = primary_by_cat.get(card_cat, {})
        chips = [
            {"label": primary_keys[k], "value": _spec_scalar(specs[k])}
            for k in primary_keys
            if k in specs and _spec_scalar(specs[k]) is not None
        ]
        if not commodity and not chips:
            # No schema-known primary values for this card — first 3 scalar entries,
            # labelled by their prettified spec key.
            for k, raw in specs.items():
                val = _spec_scalar(raw)
                if val is None:
                    continue
                chips.append({"label": k.replace("_", " "), "value": val})
                if len(chips) >= 3:
                    break
        m._primary_specs = chips

    # Coverage-aware empty state: a parametric zero-result inside a commodity usually
    # means "not yet spec-enriched", not "no such parts". Coverage is computed only when
    # the nudge could render (zero results + active parametric sub_filters + commodity).
    parametric_active = bool(commodity and parsed_filters)
    spec_coverage = None
    if total == 0 and parametric_active:
        spec_coverage = get_commodity_spec_coverage(db, commodity)

    # FRU crosswalk: when the query hits fru_links (either direction), render the
    # full matrix / "Used in FRUs" section above the card results. This is the
    # destination every "/v2/materials?q=<pn>" FRU deep link promises (the search
    # panel's "View full FRU matrix" CTA and fru_section's part-navigation links) —
    # a crosswalk-only PN matches no material card, so the section must not depend
    # on card results. Both lookups are indexed point reads; non-MPN text queries
    # simply miss and render nothing.
    # The section is ADDITIVE, so the lookups get the same scoped try/except
    # search_history_panel uses — a crosswalk failure degrades to "no FRU section"
    # and must never 500 the whole materials list (the primary surface).
    fru_view = None
    fru_reverse = None
    if q:
        from ...services.fru_matrix_service import get_fru_view, get_reverse_view

        try:
            fru_view = get_fru_view(db, q)
            fru_reverse = get_reverse_view(db, q)
        except Exception:
            logger.exception("materials faceted FRU section failed q={} user={}", q, user.id)
            fru_view = None
            fru_reverse = None

    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "materials": materials,
            "q": q,
            "total": total,
            "limit": limit,
            "offset": offset,
            "commodity": commodity,
            "commodity_display": get_display_name(commodity) if commodity else "",
            "category": commodity,
            "top_categories": [],
            "interpreted_query": "",
            "faceted": True,
            "parametric_active": parametric_active,
            "spec_coverage": spec_coverage,
            "fru_view": fru_view,
            "fru_usages": fru_reverse.usages if fru_reverse else (),
            "fru_usages_total": fru_reverse.total if fru_reverse else 0,
            "fru_query": q,
        }
    )
    return template_response("htmx/partials/materials/list.html", ctx)


@router.get("/v2/partials/materials/add-form", response_class=HTMLResponse)
async def material_add_form_partial(
    request: Request,
    user: User = Depends(require_buyer),
):
    """Render the Add-part modal form (loaded into #modal-content).

    require_buyer matches POST /api/materials/add — the form must never render for a
    role whose submit would 403 (the workspace also hides the button via
    has_buyer_role).
    NOTE: must stay registered BEFORE /v2/partials/materials/{card_id} — the path
    would otherwise be captured by the card_id route.
    """
    from ..materials import render_add_modal

    return render_add_modal(request)


@router.get("/v2/partials/materials/{card_id}/enrich-status", response_class=HTMLResponse)
async def material_enrich_status_partial(
    request: Request,
    card_id: int,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Render the enrichment-status badge for the card detail header.

    While the card is still ``unenriched`` the badge polls this route every 15s
    ("Queued for enrichment"). When the on-demand enrichment lands a terminal status the
    route returns the WHOLE refreshed detail (retargeted to #main-content) so the user
    sees the new category/specs — not just a swapped badge — and answers HTTP 286 to STOP
    polling. If the background run finished blocked/no-op (which leaves the card
    ``unenriched``), it surfaces the "couldn't complete" toast once and keeps polling.
    """
    from ...constants import MaterialEnrichmentStatus
    from ...models.intelligence import MaterialCard
    from ...services import material_enrich_runs
    from ...services.material_enrich_runs import enrich_runs

    card = db.get(MaterialCard, card_id)
    if not card or card.deleted_at is not None:
        # Polling sub-resource, not a navigable page: htmx neither swaps nor cancels
        # an `every 15s` poll on a 4xx, so a 404 would leave a detail view open after
        # the card is deleted hammering this route forever. 286 stops the poll; the
        # empty body clears the badge.
        return HTMLResponse("", status_code=286)

    if card.enrichment_status != MaterialEnrichmentStatus.UNENRICHED:
        # Enrichment landed a terminal status — refresh the WHOLE detail (new category,
        # specs, badges), not just the badge, then stop polling. HX-Retarget/Reswap
        # redirect the badge poll's outerHTML swap onto the full detail surface.
        enrich_runs.clear(card_id)
        response = await material_detail_partial(request, card_id, user, db)
        response.headers["HX-Retarget"] = "#main-content"
        response.headers["HX-Reswap"] = "innerHTML"
        response.status_code = 286  # htmx's stop-polling status — the detail still swaps in.
        return response

    # Still unenriched → keep polling. If the background run finished blocked/no-op,
    # surface the existing "couldn't complete" toast ONCE (consume_outcome pops it).
    ctx = _base_ctx(request, user, "materials")
    ctx["card"] = card
    response = template_response("htmx/partials/materials/enrich_status.html", ctx)
    if enrich_runs.consume_outcome(card_id) == material_enrich_runs.BLOCKED:
        # Bridged to the global $store.toast via the showToast HX-Trigger convention.
        response.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {
                    "message": "Enrichment couldn't complete — a data source was unavailable. Try again shortly.",
                    "type": "error",
                }
            }
        )
    return response


@router.post("/v2/partials/materials/{card_id}/conflicts/{key}/accept", response_class=HTMLResponse)
async def material_conflict_accept(
    request: Request,
    card_id: int,
    key: str,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Accept a validation conflict's evidence value — a human decision.

    Writes the evidence value at manual/100 (set_category for ``category``,
    set_brand/set_manufacturer for the dual-brand columns, record_spec for spec
    keys) and clears that key's conflict entries. An optional ``source`` form field
    selects among multiple evidence entries for the key (de-dupe is per
    (key, source)); without it the highest-(tier, confidence) entry wins. Returns
    the refreshed detail partial.
    """
    from ...models.intelligence import MaterialCard
    from ...services.spec_tiers import clear_validation_conflicts, set_brand, set_category, set_manufacturer
    from ...services.spec_write_service import record_spec

    card = db.get(MaterialCard, card_id)
    if not card or card.deleted_at is not None:
        raise HTTPException(404, "Material card not found")

    entries = [c for c in (card.validation_conflicts or []) if c.get("key") == key]
    if not entries:
        raise HTTPException(404, f"No validation conflict recorded for {key!r}")

    form = await request.form()
    source = str(form.get("source") or "").strip()
    chosen = next((c for c in entries if (c.get("evidence") or {}).get("source") == source), None)
    if chosen is None:
        chosen = max(
            entries,
            key=lambda c: (
                (c.get("evidence") or {}).get("tier") or 0,
                (c.get("evidence") or {}).get("confidence") or 0.0,
            ),
        )
    value = (chosen.get("evidence") or {}).get("value")

    if key == "category":
        wrote = set_category(card, value, "manual", 1.0)
    elif key == "brand":
        wrote = set_brand(card, value, "manual", 1.0)
    elif key == "manufacturer":
        wrote = set_manufacturer(card, value, "manual", 1.0)
    else:
        wrote = record_spec(db, card.id, key, value, source="manual", confidence=1.0)
    if not wrote:
        # The accepted value could not be written — off-vocab category, schema gone
        # after a commodity flip, or enum/numeric rejection. KEEP the conflict entry
        # (it is the only persisted record of the contradiction) and surface the
        # failure instead of silently pretending the decision was applied.
        logger.warning(
            "Material card {} conflict-accept on {!r}: value {!r} could not be written — entry kept",
            card_id,
            key,
            value,
        )
        response = await material_detail_partial(request, card_id, user, db)
        response.headers["HX-Trigger"] = json.dumps(
            {
                "showToast": {
                    "message": (
                        f'Couldn\'t apply "{value}" to {key} — the value no longer fits '
                        "this card's schema. The conflict was kept."
                    ),
                    "type": "warning",
                }
            }
        )
        return response
    clear_validation_conflicts(card, key)
    db.commit()
    logger.info("Material card {} conflict on {!r} accepted ({!r}) by {}", card_id, key, value, user.email)
    return await material_detail_partial(request, card_id, user, db)


@router.get("/v2/partials/materials/fru-lookup", response_class=HTMLResponse)
async def fru_lookup_partial(
    request: Request,
    q: str = Query("", max_length=100),
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """FRU crosswalk lookup: render whichever view matches the part number.

    Forward view when q is a known FRU, reverse "Used in FRUs" view when q appears
    as a related PN (11S/model/tray/...), an empty state when neither.
    NOTE: must stay registered BEFORE /v2/partials/materials/{card_id} — the path
    would otherwise be captured by the card_id route.
    """
    from ...services.fru_matrix_service import get_fru_view, get_reverse_view

    reverse = get_reverse_view(db, q) if q else None
    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "fru_view": get_fru_view(db, q) if q else None,
            "fru_usages": reverse.usages if reverse else (),
            "fru_usages_total": reverse.total if reverse else 0,
            "fru_query": q,
            "show_empty": bool(q),
        }
    )
    return template_response("htmx/partials/materials/fru_section.html", ctx)


@router.get("/v2/partials/materials/{card_id}", response_class=HTMLResponse)
async def material_detail_partial(
    request: Request,
    card_id: int,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Return material card detail as HTML partial."""
    from ...models.intelligence import MaterialCard
    from ...services.fru_matrix_service import get_fru_view, get_reverse_view

    card = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.id == card_id,
            MaterialCard.deleted_at.is_(None),
        )
        .first()
    )
    if not card:
        raise HTTPException(404, "Material card not found")

    sightings = sightings_for_card(db, card_id, limit=50)
    offers = offers_for_card(db, card_id, limit=50)
    mpn = card.display_mpn or card.normalized_mpn
    reverse = get_reverse_view(db, mpn)
    ctx = _base_ctx(request, user, "materials")
    ctx.update(
        {
            "card": card,
            "sightings": sightings,
            "offers": offers,
            "fru_view": get_fru_view(db, mpn),
            "fru_usages": reverse.usages,
            "fru_usages_total": reverse.total,
        }
    )
    return template_response("htmx/partials/materials/detail.html", ctx)


@router.get(
    "/v2/partials/materials/{card_id}/tab/{tab_name}",
    response_class=HTMLResponse,
)
async def material_tab_partial(
    request: Request,
    card_id: int,
    tab_name: str,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Return a material detail tab partial."""
    from ...models.intelligence import MaterialCard, MaterialVendorHistory

    card = db.get(MaterialCard, card_id)
    if not card:
        return HTMLResponse(
            "<p class='text-gray-400 text-sm py-4 text-center'>Material not found</p>",
            status_code=404,
        )

    ctx = _base_ctx(request, user, "materials")
    ctx["card"] = card

    if tab_name == "vendors":
        ctx["vendors"] = (
            db.query(MaterialVendorHistory)
            .filter_by(material_card_id=card_id)
            .order_by(MaterialVendorHistory.last_seen.desc().nullslast())
            .all()
        )
        return template_response("htmx/partials/materials/tabs/vendors.html", ctx)
    elif tab_name == "customers":
        ctx["customers"] = customer_purchases_for_card(db, card_id, limit=200)
        return template_response("htmx/partials/materials/tabs/customers.html", ctx)
    elif tab_name == "sourcing":
        ctx["requirements"] = requirements_for_card(db, card_id, limit=200)
        return template_response("htmx/partials/materials/tabs/sourcing.html", ctx)
    elif tab_name == "price_history":
        from ...models.price_snapshot import MaterialPriceSnapshot

        ctx["snapshots"] = (
            db.query(MaterialPriceSnapshot)
            .filter_by(material_card_id=card_id)
            .order_by(MaterialPriceSnapshot.recorded_at.desc())
            .limit(200)
            .all()
        )
        return template_response("htmx/partials/materials/tabs/price_history.html", ctx)
    elif tab_name == "files":
        return template_response("htmx/partials/materials/tabs/files.html", ctx)
    else:
        return HTMLResponse(
            "<p class='text-gray-400 text-sm py-4 text-center'>Unknown tab</p>",
            status_code=404,
        )


@router.put("/v2/partials/materials/{card_id}", response_class=HTMLResponse)
async def update_material_card(
    request: Request,
    card_id: int,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Update material card fields.

    Returns refreshed detail.
    """
    from ...models.intelligence import MaterialCard

    card = (
        db.query(MaterialCard)
        .filter(
            MaterialCard.id == card_id,
            MaterialCard.deleted_at.is_(None),
        )
        .first()
    )
    if not card:
        raise HTTPException(404, "Material card not found")

    form = await request.form()
    updatable = [
        "description",
        "package_type",
        "lifecycle_status",
        "rohs_status",
        "pin_count",
    ]
    for field in updatable:
        if field in form:
            val = form[field].strip() if form[field] else None
            if field == "pin_count" and val:
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    val = None
            setattr(card, field, val or None)

    # Manufacturer is a PROVENANCED column (dual-brand, migration 097) — NEVER raw
    # setattr: a raw write leaves NULL provenance, ranks at the legacy floor (50), and
    # the next decode (85) / trio re-ingest (95) silently reverts the human's edit.
    # Same contract as routers/materials.py::update_material — through the F1 ladder
    # at manual/100 (canonicalized via the alias table), with the same conflict-
    # clearing semantics as the category path below.
    manufacturer_toast: str | None = None
    if "manufacturer" in form:
        from ...services.manufacturer_normalizer import normalize_brand_name
        from ...services.spec_tiers import clear_validation_conflicts, set_manufacturer

        raw_manufacturer = (str(form["manufacturer"]) if form["manufacturer"] else "").strip()
        if raw_manufacturer:
            # A PUT carrying a non-empty maker is a re-assertion — clear any recorded
            # validation conflict for it (even unchanged: the human looked and
            # confirmed their value), mirroring the category path below.
            clear_validation_conflicts(card, "manufacturer")
            # Canonical-to-CANONICAL comparison (exact match short-circuits the alias
            # lookups): legacy cards store non-canonical aliases ("TI", "HP" — the
            # stored value pre-dates the ladder), and the edit form round-trips the
            # stored value verbatim — comparing canonical(incoming) against the RAW
            # stored value would see "Texas Instruments" != "TI" on every unrelated
            # save and silently re-stamp the maker as manual (tier 100), locking out
            # every future enrichment correction.
            if raw_manufacturer != (card.manufacturer or "") and normalize_brand_name(
                db, raw_manufacturer
            ) != normalize_brand_name(db, card.manufacturer or ""):
                set_manufacturer(card, raw_manufacturer, "manual", 1.0)
            # Canonical-equal → no-op: an unchanged value must NOT be re-stamped as a
            # manual (tier 100) edit just because the user saved another field.
        elif card.manufacturer:
            # Empty/whitespace → no-op: the ladder never blanks a value
            # (set_manufacturer contract — the old raw write could silently blank the
            # maker here). Tell the user instead of silently dropping the edit,
            # mirroring the category blank-rejection toast below.
            manufacturer_toast = f'Manufacturer can\'t be cleared — kept "{card.manufacturer}".'

    # Category NEVER goes through raw setattr: a raw write would leave the OLD
    # provenance columns attached to the NEW value (the next enrichment pass would
    # silently revert the human's correction), skip the stale-commodity facet purge,
    # and persist off-vocab free text. Route it through the F1 ladder instead —
    # "manual" is tier 100, so a deliberate human change always wins, gets provenance
    # stamped, and purges the old commodity's facets.
    category_toast: str | None = None
    if "category" in form:
        from ...services.category_normalizer import normalize_category
        from ...services.spec_tiers import clear_validation_conflicts, set_category

        raw_category = (str(form["category"]) if form["category"] else "").strip()
        canonical = normalize_category(raw_category)
        if canonical is not None:
            # A PUT carrying a canonical category is a re-assertion — clear any
            # recorded validation conflict for it (even unchanged: the human looked
            # and confirmed their value).
            clear_validation_conflicts(card, "category")
        if canonical is not None and canonical != card.category:
            set_category(card, canonical, "manual", 1.0)
        elif canonical is None and raw_category:
            # Off-vocab free text — never persisted (it would be invisible to every
            # commodity filter). Tell the user instead of silently dropping the edit.
            category_toast = (
                f'Category "{raw_category}" is not a recognized commodity — kept '
                f'"{card.category or "none"}". Use a canonical key like hdd, ssd or dram.'
            )
        elif not raw_category and card.category:
            # The ladder never blanks an existing category (set_category contract).
            category_toast = f'Category can\'t be cleared — kept "{card.category}".'
        # canonical == card.category → no-op: an unchanged value must NOT be re-stamped
        # as a manual (tier 100) edit just because the user saved another field.

    db.commit()
    logger.info("Material card {} updated by {}", card_id, user.email)
    response = await material_detail_partial(request, card_id, user, db)
    toast_messages = [m for m in (category_toast, manufacturer_toast) if m]
    if toast_messages:
        # Surface the rejection(s) WITHOUT breaking the partial swap, via the existing
        # showToast HX-Trigger convention bridged to $store.toast (htmx_app.js).
        # HX-Trigger is a single JSON event map, so both rejections share one toast.
        response.headers["HX-Trigger"] = json.dumps(
            {"showToast": {"message": " ".join(toast_messages), "type": "warning"}}
        )
    return response


async def _run_card_enrichment(material_id: int) -> None:
    """Background worker: run the authoritative ladder + structured-spec pass for one card.

    Scheduled by ``enrich_material`` so the click never blocks on the ~30s of web
    extraction. Runs the authoritative ladder (verified -> web -> OEM -> flagged
    inference) with refresh=True so even a terminal card re-enters the ladder, then a
    status-gated structured-spec pass. The Haiku card-enrichment path was removed in SP1
    (2026-06-09). Records the run's outcome in ``enrich_runs`` so the enrich-status poller
    can surface the "couldn't complete" toast on a blocked/no-op run (which leaves the
    card ``unenriched``, indistinguishable from success by the status column alone).

    Opens its own session — FastAPI has already returned the response and closed the
    request session by the time this runs. Must NEVER raise: it is a fire-and-forget task.
    """
    from ...constants import MaterialEnrichmentStatus
    from ...database import SessionLocal
    from ...services.material_enrich_runs import enrich_runs

    db = SessionLocal()
    blocked = False
    try:
        # enrich_cards self-handles ClaudeError / disabled-source outages internally and
        # returns a counts dict (it does NOT raise on a backend outage). Capture it so the
        # poller can tell the user when nothing actually happened, not report false success.
        counts: dict = {}
        try:
            from ...services.authoritative_enrichment_service import enrich_cards

            counts = await enrich_cards([material_id], db, refresh=True)
        except Exception as e:
            logger.exception("Enrichment failed for material {}: {}", material_id, e)
            blocked = True

        # A single card produces exactly one status tally on success. If no real status
        # landed, or a Claude outage / disabled source blocked the run, the card is unchanged.
        status_tallies = sum(int(counts.get(s, 0)) for s in MaterialEnrichmentStatus)
        if counts.get("claude_error") or counts.get("disabled_sources") or status_tallies == 0:
            blocked = True
            logger.warning("Enrichment no-op for material {} (counts={})", material_id, counts)

        try:
            from ...services.spec_enrichment_service import enrich_card_specs

            await enrich_card_specs([material_id], db, force=True)
        except Exception as e:  # noqa: BLE001 — card-level enrichment may still have succeeded
            logger.warning("Spec enrichment failed for material {}: {}", material_id, e)
    except Exception:  # noqa: BLE001 — a background task must not crash the worker
        logger.exception("Card enrichment task crashed for material {}", material_id)
        blocked = True
    finally:
        db.close()
        enrich_runs.finish(material_id, blocked=blocked)


@router.post("/v2/partials/materials/{material_id}/enrich", response_class=HTMLResponse)
async def enrich_material(
    request: Request,
    material_id: int,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Queue authoritative enrichment for a material card and return immediately.

    The heavy work (authoritative ladder + structured-spec pass, ~30s of web extraction)
    runs in a FastAPI background task so the click never blocks. The card is flipped to
    the ``unenriched`` ("Queued for enrichment") marker and the detail partial is returned
    right away with the enrich-status badge polling; that poller lands the refreshed detail
    on success — or the "couldn't complete" toast on a blocked run — when the task finishes.
    """
    from ...constants import MaterialEnrichmentStatus
    from ...models.intelligence import MaterialCard
    from ...services.material_enrich_runs import enrich_runs

    mc = db.get(MaterialCard, material_id)
    if not mc:
        raise HTTPException(404, "Material not found")

    # Guard double-enqueue: a run already in flight for this card must not stack another.
    if enrich_runs.begin(material_id):
        # Flip to the queued/in-progress marker so the badge polls while the worker runs
        # (also resets an already-terminal card so its poller re-activates on re-enrich).
        mc.enrichment_status = MaterialEnrichmentStatus.UNENRICHED
        db.commit()
        background_tasks.add_task(_run_card_enrichment, material_id)

    return await material_detail_partial(request, material_id, user, db)


async def _run_card_crosses(material_id: int) -> None:
    """Background worker: run the AI crosses/substitutes lookup for one material card.

    Scheduled by ``find_crosses`` so the click never blocks on the ~30s Claude call. Opens
    its own session (FastAPI has already returned the response and closed the request
    session by the time this runs), persists the deduplicated crosses onto the card, and
    records the run's outcome in ``crosses_runs`` so the crosses-status poller can swap in
    the results (``done``) or show the retry/error state (``blocked``). Because a
    legitimate no-results run leaves ``cross_references`` empty — indistinguishable from
    "never ran" by the column alone — the registry outcome is what the poller trusts.

    Must NEVER raise: it is a fire-and-forget task.
    """
    from ...database import SessionLocal
    from ...models.intelligence import MaterialCard
    from ...services.material_enrich_runs import crosses_runs
    from ...utils.claude_client import claude_json as ai_json
    from ...utils.normalization import normalize_mpn_key

    db = SessionLocal()
    blocked = False
    try:
        mc = db.get(MaterialCard, material_id)
        if not mc:
            blocked = True
            return

        mpn = mc.display_mpn or mc.normalized_mpn
        mfg = mc.manufacturer or "unknown"
        category = mc.category or "electronic component"

        import asyncio as _asyncio

        result = await _asyncio.wait_for(
            ai_json(
                f"List all known CROSSES and SUBSTITUTES for this electronic component:\n"
                f"  MPN: {mpn}\n"
                f"  Manufacturer: {mfg}\n"
                f"  Category: {category}\n\n"
                f"Include:\n"
                f"1. Cross-manufacturer equivalents\n"
                f"2. Pin-compatible alternatives / clones\n"
                f"3. Same-family variants (different speed grades, temp ranges, packages)\n"
                f"4. Second-source parts\n\n"
                f"Only include REAL part numbers you are confident exist. Up to 10 results.\n\n"
                f'Respond with JSON: {{"crosses": [{{"mpn": "...", "manufacturer": "..."}}]}}',
                system=(
                    "You are an expert electronic component sourcing engineer. "
                    "List real, verified part numbers only — no guessing."
                ),
                model_tier="smart",
                max_tokens=2048,
            ),
            timeout=30.0,
        )

        crosses = result.get("crosses", []) if isinstance(result, dict) else []
        # Deduplicate: exclude the card's own MPN (both display and normalized forms)
        own_mpns = {normalize_mpn_key(mc.normalized_mpn or ""), normalize_mpn_key(mc.display_mpn or "")} - {""}
        crosses = [
            c for c in crosses if isinstance(c, dict) and c.get("mpn") and normalize_mpn_key(c["mpn"]) not in own_mpns
        ]

        mc.cross_references = crosses
        db.commit()
    except Exception as exc:  # noqa: BLE001 — a background task must not crash the worker
        logger.warning("Cross-reference search failed for material {}: {}", material_id, exc)
        db.rollback()
        blocked = True
    finally:
        db.close()
        crosses_runs.finish(material_id, blocked=blocked)


@router.post("/v2/partials/materials/{material_id}/find-crosses", response_class=HTMLResponse)
async def find_crosses(
    request: Request,
    material_id: int,
    background_tasks: BackgroundTasks,
    refresh: bool = Form(False),
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Queue the on-demand AI crosses/substitutes lookup and return immediately.

    The Claude call (``asyncio.wait_for`` up to 30s) used to run INLINE before responding,
    so the Crosses section spun and the page felt frozen. It now runs in a FastAPI
    background task and this handler returns the "Finding crosses…" polling partial right
    away; the crosses-status poller swaps in the results (or the retry state) when the task
    finishes. A cache hit (already-populated ``cross_references`` and no explicit refresh)
    still returns the loaded section synchronously — no background work needed.

    Called by: HTMX button on the material detail Crosses section.
    Depends on: _run_card_crosses (background worker), crosses_runs (double-enqueue guard).
    """
    from ...models.intelligence import MaterialCard
    from ...services.material_enrich_runs import crosses_runs

    mc = db.get(MaterialCard, material_id)
    if not mc:
        raise HTTPException(404, "Material not found")

    # Cache hit: return the loaded section immediately (skip on explicit refresh).
    if mc.cross_references and not refresh:
        return template_response(
            "htmx/partials/materials/crosses_section.html",
            {"request": request, "card": mc},
        )

    # Guard double-enqueue: a lookup already in flight for this card must not stack another.
    if crosses_runs.begin(material_id):
        background_tasks.add_task(_run_card_crosses, material_id)

    # Return the polling in-progress state immediately (no inline 30s block).
    return template_response(
        "htmx/partials/materials/crosses_status.html",
        {"request": request, "card": mc},
    )


@router.get("/v2/partials/materials/{card_id}/crosses-status", response_class=HTMLResponse)
async def material_crosses_status_partial(
    request: Request,
    card_id: int,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Poll the in-flight AI crosses lookup and swap in the result when it lands.

    While the background lookup is running this returns the "Finding crosses…" polling
    partial (keep polling). On the terminal outcome it returns the refreshed
    ``crosses_section.html`` (loaded results, empty "none found", or — on a blocked run —
    the retry/error state) and answers HTTP 286 so htmx swaps the section and STOPS
    polling. If no run is tracked (e.g. the process restarted mid-run) it stops polling and
    renders the card's current section rather than spinning forever.
    """
    from ...models.intelligence import MaterialCard
    from ...services import material_enrich_runs
    from ...services.material_enrich_runs import crosses_runs

    card = db.get(MaterialCard, card_id)
    if not card or card.deleted_at is not None:
        # Polling sub-resource: htmx neither swaps nor cancels an `every Ns` poll on a 4xx,
        # so a 404 would hammer this route forever. 286 stops the poll; empty body clears it.
        return HTMLResponse("", status_code=286)

    outcome = crosses_runs.consume_outcome(card_id)
    if outcome is None and crosses_runs.is_running(card_id):
        # Still running → keep polling.
        return template_response(
            "htmx/partials/materials/crosses_status.html",
            {"request": request, "card": card},
        )

    # Terminal (done / blocked) or no tracked run → swap in the section and stop polling.
    ctx = {"request": request, "card": card}
    if outcome == material_enrich_runs.BLOCKED:
        ctx["error"] = "Cross-reference search failed. Please try again."
    response = template_response("htmx/partials/materials/crosses_section.html", ctx)
    response.status_code = 286  # htmx's stop-polling status — the section still swaps in.
    return response


@router.get("/v2/partials/materials/{material_id}/insights", response_class=HTMLResponse)
async def material_insights(
    request: Request,
    material_id: int,
    user: User = Depends(require_access(AccessKey.MATERIALS)),
    db: Session = Depends(get_db),
):
    """Return MPN insights panel for a material card."""
    from ...models.intelligence import MaterialCard

    mc = db.query(MaterialCard).filter(MaterialCard.id == material_id).first()
    if not mc:
        raise HTTPException(404, "Material not found")

    # Get related offers for pricing data
    offers = (
        (
            db.query(Offer)
            .filter(Offer.normalized_mpn == mc.normalized_mpn, Offer.unit_price.isnot(None))
            .order_by(Offer.created_at.desc())
            .limit(20)
            .all()
        )
        if mc.normalized_mpn
        else []
    )

    return template_response(
        "htmx/partials/materials/insights.html",
        {"request": request, "material": mc, "offers": offers},
    )

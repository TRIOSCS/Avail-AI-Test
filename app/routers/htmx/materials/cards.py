"""app/routers/htmx/materials/cards.py — Material card surface — CSV export, add-form,
enrich-status poller, conflict-accept, FRU lookup, detail panel + tabs, card update.

W4.8 split of the 1,483-line app/routers/htmx/materials.py — pure structural
move: URLs and behavior unchanged; every route attaches to the shared router
imported from .common (registration assembled in __init__).
"""

import json

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ....constants import (
    AccessKey,
)
from ....database import get_db
from ....dependencies import (
    require_access,
    require_buyer,
)
from ....models import (
    User,
)
from ....services.faceted_search_service import (
    search_materials_faceted,
)
from ....services.part_history_service import (
    customer_purchases_for_card,
    offers_for_card,
    requirements_for_card,
    sightings_for_card,
)
from ....template_env import template_response
from ....utils.csv_export import stream_csv
from .._shared import _base_ctx
from .common import _parse_card_filter_params, _parse_filter_json, _pop_manufacturers, router

# CSV export column order — the real MaterialCard fields the buyer scans on the results list.
_MATERIALS_EXPORT_COLUMNS = [
    "MPN",
    "Manufacturer",
    "Category",
    "Package",
    "Lifecycle",
    "Enrichment Status",
    "Vendor Count",
    "Best Price",
    "Created",
    "Updated",
]


def _materials_export_rows(db, *, commodity, q, sub_filters, manufacturers, verified_only, card_params):
    """Yield one CSV row per faceted-search match (header handled by stream_csv).

    Pages through ``search_materials_faceted`` — the SAME filtered query the results list
    runs — in chunks so the export can never diverge from the visible list, batch-loading
    each chunk's vendor stats (count + min recorded price) exactly the way
    ``materials_faceted_partial`` does. No pagination cap on the exported set.
    """
    from ....models.intelligence import MaterialVendorHistory

    chunk = 500
    offset = 0
    while True:
        materials, _total = search_materials_faceted(
            db,
            commodity=commodity,
            q=q,
            sub_filters=sub_filters,
            manufacturers=manufacturers,
            verified_only=verified_only,
            **card_params,
            limit=chunk,
            offset=offset,
        )
        if not materials:
            break
        card_ids = [m.id for m in materials]
        vendor_stats: dict[int, tuple[int, object]] = {}
        for cid, cnt, best in (
            db.query(
                MaterialVendorHistory.material_card_id,
                sqlfunc.count(MaterialVendorHistory.id),
                sqlfunc.min(MaterialVendorHistory.last_price),
            )
            .filter(MaterialVendorHistory.material_card_id.in_(card_ids))
            .group_by(MaterialVendorHistory.material_card_id)
            .all()
        ):
            vendor_stats[cid] = (cnt, best)
        for m in materials:
            cnt, best = vendor_stats.get(m.id, (0, None))
            yield (
                m.display_mpn or m.normalized_mpn or "",
                m.manufacturer or "",
                m.category or "",
                m.package_type or "",
                m.lifecycle_status or "",
                m.enrichment_status or "",
                cnt,
                best if best is not None else "",
                m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
                m.updated_at.strftime("%Y-%m-%d %H:%M") if m.updated_at else "",
            )
        if len(materials) < chunk:
            break
        offset += chunk


@router.get("/v2/partials/materials/export")
async def materials_export(
    commodity: str = "",
    q: str = "",
    sub_filters: str = "{}",
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
    """Stream every faceted-search-matching material card as a CSV download (attachment,
    no pagination).

    Same auth (MATERIALS access) and the same faceted filter params as the results list —
    the export mirrors the visible list (commodity / search / manufacturers / sub-filters /
    trust + global + sourcing facets) via the shared ``search_materials_faceted``, so the
    download can never drift from what the list shows. Shares the exact degrade-don't-500
    param parsing (``_parse_card_filter_params`` / ``_parse_filter_json`` /
    ``_pop_manufacturers``) with the list and the sidebar count routes.

    NOTE: must stay registered BEFORE /v2/partials/materials/{card_id} — the path would
    otherwise be captured by the card_id route.
    """
    parsed_filters = _parse_filter_json(sub_filters, coerce_numeric=True)
    manufacturers = _pop_manufacturers(parsed_filters)
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
    rows = _materials_export_rows(
        db,
        commodity=commodity or None,
        q=q or None,
        sub_filters=parsed_filters or None,
        manufacturers=manufacturers,
        verified_only=verified_only,
        card_params=card_params,
    )
    return stream_csv("materials_export.csv", _MATERIALS_EXPORT_COLUMNS, rows)


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
    from ...materials import render_add_modal

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
    from ....constants import MaterialEnrichmentStatus
    from ....models.intelligence import MaterialCard
    from ....services import material_enrich_runs
    from ....services.material_enrich_runs import enrich_runs

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
    from ....models.intelligence import MaterialCard
    from ....services.spec_tiers import clear_validation_conflicts, set_brand, set_category, set_manufacturer
    from ....services.spec_write_service import record_spec

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
    from ....services.fru_matrix_service import get_fru_view, get_reverse_view

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
    from ....models.intelligence import MaterialCard
    from ....services.fru_matrix_service import get_fru_view, get_reverse_view

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
    from ....models.intelligence import MaterialCard, MaterialVendorHistory

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
        from ....models.price_snapshot import MaterialPriceSnapshot

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
    from ....models.intelligence import MaterialCard

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
        from ....services.manufacturer_normalizer import normalize_brand_name
        from ....services.spec_tiers import clear_validation_conflicts, set_manufacturer

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
        from ....services.category_normalizer import normalize_category
        from ....services.spec_tiers import clear_validation_conflicts, set_category

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

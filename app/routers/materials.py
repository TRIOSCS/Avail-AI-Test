"""routers/materials.py — Material Card CRUD and stock import.

Handles material card listing, detail, add, update, soft-delete,
and standalone stock list import.

Called by: main.py (router mount)
Depends on: models, dependencies, stock_list_ingest, cache, normalization, audit_service
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..cache.decorators import cached_endpoint, invalidate_prefix
from ..database import get_db
from ..dependencies import require_admin, require_buyer, require_user
from ..models import (
    MaterialCard,
    MaterialVendorHistory,
    Offer,
    User,
    VendorCard,
)
from ..schemas.vendors import MaterialCardUpdate
from ..services.credential_service import get_credential_cached
from ..services.material_card_service import (
    backfill_missing_manufacturers,
)
from ..services.material_card_service import (
    infer_manufacturer as _infer_manufacturer_from_prefix,
)
from ..services.material_card_service import (
    serialize_material_card as material_card_to_dict,
)
from ..services.spec_tiers import set_manufacturer
from ..services.stock_list_ingest import (
    StockListResult,
    StockListValidationError,
    ingest_stock_list,
    validate_metadata,
)
from ..utils.async_helpers import safe_background_task
from ..utils.normalization import normalize_mpn_key
from ..utils.search_builder import SearchBuilder
from ..utils.vendor_helpers import _background_enrich_vendor

router = APIRouter(tags=["vendors"])


def _stamp_manual_provenance(card: MaterialCard, fields: list[str]) -> None:
    """Stamp manual/100 (confidence 1.0) per-field provenance entries on the card.

    Every human-supplied field write (Add-part modal, PUT update) records where the
    value came from, so the enrichment worker / spec passes can rank it on the F1 ladder
    and the validation contract can detect contradictions later.
    """
    if not fields:
        return
    now_iso = datetime.now(UTC).isoformat()
    prov = dict(card.enrichment_provenance or {})
    for field in fields:
        prov[field] = {"source": "manual", "tier": 100, "confidence": 1.0, "fetched_at": now_iso}
    card.enrichment_provenance = prov


def _actor_email(user: User) -> str:
    """Audit/merge actor label — the user's email, or ``"admin"`` if it's unset."""
    return user.email if hasattr(user, "email") else "admin"


def _backfill_manufacturer(card: MaterialCard, db: Session) -> None:
    """Fill a card's missing manufacturer from its MPN prefix, committing on a hit.

    Used by the single-card read endpoint (by-id) so a card surfaced without a
    manufacturer gets one inferred lazily on first view.
    """
    if card.manufacturer:
        return
    inferred = _infer_manufacturer_from_prefix(db, card.normalized_mpn)
    if inferred:
        card.manufacturer = inferred
        db.add(card)
        db.commit()
        invalidate_prefix("material_list")


def render_add_modal(
    request: Request, *, errors: dict | None = None, values: dict | None = None, status_code: int = 200
):
    """Render the Add-part modal partial (shared by the GET form route and the 422 re-
    render of POST /api/materials/add)."""
    from ..constants import MaterialCondition
    from ..services.commodity_registry import get_all_commodities, get_display_name
    from ..template_env import template_response

    ctx = {
        "request": request,
        "commodities": sorted(((k, get_display_name(k)) for k in get_all_commodities()), key=lambda kv: kv[1]),
        "conditions": [c.value for c in MaterialCondition],
        "errors": errors or {},
        "values": values or {},
    }
    return template_response("htmx/partials/materials/add_modal.html", ctx, status_code=status_code)


# -- Material Card CRUD -------------------------------------------------------


@router.post("/api/materials/add")
async def add_material(
    request: Request,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    """Single-part Add — create (or resolve) a MaterialCard from the Add-part modal.

    Fields (exactly five): mpn (required), manufacturer, description, category,
    condition. User-supplied values enter the F1 ladder at manual/100; blank = blank
    (omitted fields stay NULL for enrichment to fill — never defaulted or guessed).
    On create: runs the three inline deterministic passes (decode / FRU crosswalk /
    desc-parse) in this request, then stamps ``enrich_requested_at`` so the enrichment
    worker's priority lane picks the card up next (single-add only — bulk/stock
    imports never stamp). V3 intake validation is blocking: invalid MPN / category /
    condition → 422 with the modal re-rendered carrying per-field messages.
    Success → HX-Redirect to the card detail.
    """
    from ..constants import MaterialCondition, MaterialEnrichmentStatus
    from ..search_service import resolve_material_card, run_deterministic_passes
    from ..services.category_normalizer import normalize_category
    from ..services.spec_tiers import clear_validation_conflicts, set_category
    from ..utils.normalization import normalize_mpn

    form = await request.form()
    mpn = str(form.get("mpn") or "").strip()
    manufacturer = str(form.get("manufacturer") or "").strip()
    description = str(form.get("description") or "").strip()
    category = str(form.get("category") or "").strip()
    condition = str(form.get("condition") or "").strip()

    # V3 intake validation — blocking, never silent. 422 re-renders the modal with
    # per-field messages (htmx_app.js allows 422 swaps into #modal-content).
    errors: dict[str, str] = {}
    if not normalize_mpn(mpn):
        errors["mpn"] = "Enter a valid MPN (at least 3 characters)."
    canonical_category = None
    if category:
        canonical_category = normalize_category(category)
        if canonical_category is None:
            errors["category"] = f'"{category}" is not a recognized commodity.'
    if condition:
        try:
            condition = MaterialCondition(condition).value
        except ValueError:
            errors["condition"] = f'"{condition}" is not a valid condition.'
    values = {
        "mpn": mpn,
        "manufacturer": manufacturer,
        "description": description,
        "category": category,
        "condition": condition,
    }
    if errors:
        return render_add_modal(request, errors=errors, values=values, status_code=422)

    norm = normalize_mpn_key(mpn)
    created = (
        db.query(MaterialCard.id).filter_by(normalized_mpn=norm).filter(MaterialCard.deleted_at.is_(None)).first()
        is None
    )
    card = resolve_material_card(mpn, db)
    if card is None:
        # Reachable: normalize_mpn (display normalizer, keeps punctuation) passed but
        # normalize_mpn_key (dedup key, strips ALL non-alphanumerics) emptied — e.g. a
        # punctuation-only "MPN" like "---". Re-render the modal like every other 422;
        # a raw HTTPException body would be swapped into #modal-content as JSON text
        # (htmx_app.js force-allows 422 swaps targeted there).
        return render_add_modal(request, errors={"mpn": "Enter a valid MPN."}, values=values, status_code=422)

    # Manual/100 writes — blank = blank (never default, suggest, or copy values).
    written: list[str] = []
    if manufacturer:
        # Through the F1 ladder at manual/100 — same durability contract as the PUT
        # path: a direct write would leave NULL provenance (legacy floor 50) and be
        # silently reverted by the next decode/ingest. Canonicalizes via the alias table.
        if set_manufacturer(card, manufacturer, "manual", 1.0):
            written.append("manufacturer")
            # A manual (re-)assertion resolves any recorded manufacturer conflict —
            # same clearing semantics as category below.
            clear_validation_conflicts(card, "manufacturer")
    if description:
        card.description = description
        written.append("description")
    if condition:
        card.condition = condition  # validated MaterialCondition vocabulary above
        written.append("condition")
    if canonical_category:
        if set_category(card, canonical_category, "manual", 1.0):
            written.append("category")
            # A manual (re-)assertion resolves any recorded category conflict — same
            # clearing semantics as both PUT paths (a re-add through the modal is a
            # re-assertion too; the stale "Needs review" badge must not survive it).
            clear_validation_conflicts(card, "category")
    if written:
        _stamp_manual_provenance(card, written)
        if not card.enrichment_source:
            card.enrichment_source = "manual"

    db.flush()
    # Inline deterministic passes — decoded facets/category are visible in the create
    # response, before the worker ever sees the card.
    run_deterministic_passes(db, [card.id])
    # Priority lane: single-add only — a user is actively waiting on this card. The
    # worker FIFOs stamped cards ahead of the backlog and clears the stamp per batch.
    # Stamp ONLY cards select_batch can actually pick (unenriched / not_found /
    # not_catalogued): run_one_batch clearing is the sole clearing mechanism, so a
    # stamp on an already-enriched re-add would persist forever and front-run the
    # FIFO if the card ever re-entered eligibility.
    if card.enrichment_status in (
        MaterialEnrichmentStatus.UNENRICHED,
        MaterialEnrichmentStatus.NOT_FOUND,
        MaterialEnrichmentStatus.NOT_CATALOGUED,
    ):
        card.enrich_requested_at = datetime.now(UTC)
    db.commit()
    invalidate_prefix("material_list")

    response = JSONResponse({"ok": True, "card_id": card.id, "created": created})
    # The modal redirects to the card detail (full-page deep link).
    response.headers["HX-Redirect"] = f"/v2/materials/{card.id}"
    return response


@router.get("/api/materials")
async def list_materials(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = request.query_params.get("q", "").strip().lower()
    try:
        limit = min(int(request.query_params.get("limit", "200")), 1000)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, "limit and offset must be integers") from e

    if q and len(q) < 2:
        req_id = getattr(request.state, "request_id", "unknown")
        return JSONResponse(
            status_code=400,
            content={
                "error": "Search query must be at least 2 characters",
                "status_code": 400,
                "request_id": req_id,
            },
        )

    @cached_endpoint(prefix="material_list", ttl_hours=2, key_params=["q", "limit", "offset"])
    def _fetch(q, limit, offset, user, db):
        query = (
            db.query(MaterialCard)
            .filter(MaterialCard.deleted_at.is_(None))
            .order_by(MaterialCard.last_searched_at.desc())
        )
        if q:
            sb = SearchBuilder(q)
            query = query.filter(sb.ilike_filter(MaterialCard.normalized_mpn, prefix=True))
        total = query.count()
        cards = query.limit(limit).offset(offset).all()
        if not cards:
            return {"materials": [], "total": total, "limit": limit, "offset": offset}
        # Batch fetch vendor counts -- single query instead of N+1
        card_ids = [c.id for c in cards]
        counts = (
            dict(
                db.query(
                    MaterialVendorHistory.material_card_id,
                    sqlfunc.count(MaterialVendorHistory.id),
                )
                .filter(MaterialVendorHistory.material_card_id.in_(card_ids))
                .group_by(MaterialVendorHistory.material_card_id)
                .all()
            )
            if card_ids
            else {}
        )
        # Batch fetch top brand tag per card
        from ..models.tags import MaterialTag, Tag

        brand_tags = {}
        if card_ids:
            brand_rows = (
                db.query(
                    MaterialTag.material_card_id,
                    Tag.name,
                    MaterialTag.confidence,
                )
                .join(Tag, MaterialTag.tag_id == Tag.id)
                .filter(
                    MaterialTag.material_card_id.in_(card_ids),
                    Tag.tag_type == "brand",
                    MaterialTag.confidence >= 0.70,
                )
                .order_by(MaterialTag.confidence.desc())
                .all()
            )
            for mid, name, conf in brand_rows:
                if mid not in brand_tags:  # keep highest confidence
                    brand_tags[mid] = {"name": name, "confidence": round(float(conf), 2)}
        # Batch fetch offer counts + best price
        offer_stats = {}
        if card_ids:
            rows = (
                db.query(
                    Offer.material_card_id,
                    sqlfunc.count(Offer.id),
                    sqlfunc.min(Offer.unit_price),
                )
                .filter(Offer.material_card_id.in_(card_ids))
                .group_by(Offer.material_card_id)
                .all()
            )
            for mid, cnt, minp in rows:
                offer_stats[mid] = {"count": cnt, "best_price": float(minp) if minp else None}
        return {
            "materials": [
                {
                    "id": c.id,
                    "display_mpn": c.display_mpn,
                    "manufacturer": c.manufacturer,
                    "search_count": c.search_count or 0,
                    "vendor_count": counts.get(c.id, 0),
                    "offer_count": offer_stats.get(c.id, {}).get("count", 0),
                    "best_price": offer_stats.get(c.id, {}).get("best_price"),
                    "last_searched_at": c.last_searched_at.isoformat() if c.last_searched_at else None,
                    "brand_tag": brand_tags.get(c.id),
                }
                for c in cards
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    return _fetch(q=q, limit=limit, offset=offset, user=user, db=db)


@router.get("/api/materials/{card_id}")
async def get_material(card_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    card = db.get(MaterialCard, card_id)
    if not card or card.deleted_at is not None:
        raise HTTPException(404, "Material not found")
    _backfill_manufacturer(card, db)
    return material_card_to_dict(card, db)


@router.post("/api/quick-search")
async def quick_search(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Ad-hoc MPN search — hit supplier APIs for a single part number.

    Quick check for sightings and offer history without creating a requisition.
    Called by: frontend API button in intake bar.
    Depends on: search_service.quick_search_mpn
    """
    body = await request.json()
    mpn = (body.get("mpn") or "").strip()
    if not mpn:
        raise HTTPException(400, "MPN is required")
    if len(mpn) < 2:
        raise HTTPException(400, "MPN must be at least 2 characters")

    from ..search_service import quick_search_mpn

    result = await quick_search_mpn(mpn, db)
    return result


@router.put("/api/materials/{card_id}")
async def update_material(
    card_id: int,
    data: MaterialCardUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..services.spec_tiers import clear_validation_conflicts, set_category

    card = db.get(MaterialCard, card_id)
    if not card or card.deleted_at is not None:
        raise HTTPException(404, "Material not found")
    written: list[str] = []
    if data.manufacturer is not None:
        # Through the F1 ladder at manual/100 (the top tier): a human correction must be
        # DURABLE — a direct `card.manufacturer = ...` write would leave NULL provenance,
        # rank at the legacy floor (50), and be silently reverted by the next decode (85)
        # or trio re-ingest (95). set_manufacturer also canonicalizes via the alias table
        # and rejects empty/whitespace (a write can never blank a value).
        if set_manufacturer(card, data.manufacturer, "manual", 1.0):
            written.append("manufacturer")
            # A manual (re-)assertion resolves any recorded manufacturer conflict —
            # same clearing contract as category below.
            clear_validation_conflicts(card, "manufacturer")
    if data.description is not None:
        card.description = data.description
        written.append("description")
    if data.display_mpn is not None and data.display_mpn.strip():
        card.display_mpn = data.display_mpn.strip()
        written.append("display_mpn")
    # Enrichment fields. Category is handled separately below — NEVER via raw setattr:
    # a raw write bypasses the F1 ladder, leaving the OLD provenance columns attached
    # to the NEW value (the next enrichment pass would silently revert the human's
    # correction) and skipping the stale-commodity facet purge.
    for field in (
        "lifecycle_status",
        "package_type",
        "rohs_status",
        "pin_count",
        "datasheet_url",
        "cross_references",
        "specs_summary",
    ):
        val = getattr(data, field, None)
        if val is not None:
            setattr(card, field, val)
            written.append(field)
    if data.category is not None:
        # F1 ladder: a human edit is manual/100 — it wins, gets provenance stamped,
        # and purges the old commodity's facets. Off-vocab values are never persisted,
        # and the JSON API must SAY so (a 200 with the edit silently dropped is
        # indistinguishable from acceptance — the htmx PUT path surfaces the same
        # rejection as a toast). 422 reverts the whole request (nothing committed).
        from ..services.category_normalizer import normalize_category

        raw_category = data.category.strip()
        canonical = normalize_category(raw_category)
        if canonical is None:
            if raw_category:
                raise HTTPException(422, f'"{raw_category}" is not a recognized commodity.')
            # The ladder never blanks an existing category (set_category contract).
            raise HTTPException(422, f'Category can\'t be cleared — kept "{card.category or "none"}".')
        if set_category(card, canonical, "manual", 1.0):
            written.append("category")
            # A canonical re-assertion clears any recorded conflict for the key
            # (even an unchanged value: the human looked and confirmed it).
            clear_validation_conflicts(card, "category")
    if written:
        _stamp_manual_provenance(card, written)
        if not card.enrichment_source:
            card.enrichment_source = "manual"
    db.commit()
    invalidate_prefix("material_list")
    return material_card_to_dict(card, db)


@router.delete("/api/materials/{card_id}")
async def delete_material(card_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Soft-delete a material card.

    Sets deleted_at timestamp; records are preserved.
    """
    from ..services.audit_service import log_audit

    card = db.get(MaterialCard, card_id)
    if not card:
        raise HTTPException(404, "Material not found")
    if card.deleted_at is not None:
        raise HTTPException(400, "Card is already deleted")
    card.deleted_at = datetime.now(UTC)
    log_audit(
        db,
        material_card_id=card.id,
        action="soft_deleted",
        normalized_mpn=card.normalized_mpn,
        created_by=_actor_email(user),
    )
    db.commit()
    invalidate_prefix("material_list")
    return {"ok": True, "deleted_at": card.deleted_at.isoformat()}


# -- Admin: Backfill Missing Manufacturers ------------------------------------


@router.post("/materials/backfill-manufacturers", tags=["admin"])
async def backfill_manufacturers(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """One-time admin endpoint to enrich all material cards missing a manufacturer via
    prefix-match."""
    count = backfill_missing_manufacturers(db)
    db.commit()
    invalidate_prefix("material_list")
    return {"enriched_records": count}


# -- Standalone Stock Import ---------------------------------------------------


@router.post("/api/materials/import-stock")
async def import_stock_list_standalone(
    request: Request, user: User = Depends(require_buyer), db: Session = Depends(get_db)
):
    """Import a vendor stock list -- stores ALL rows as MaterialCard +
    MaterialVendorHistory.

    Thin wrapper over ``stock_list_ingest.ingest_stock_list`` (the shared ingest used by
    both this JSON endpoint and the Vendors-page HTMX upload modal). Returns JSON.
    """
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "No file uploaded")

    filename = file.filename or "upload.csv"
    vendor_name = form.get("vendor_name") or ""
    try:
        # Cheap checks (type + vendor) first — reject before buffering the body.
        validate_metadata(filename, vendor_name)
        content = await file.read()
        result = ingest_stock_list(
            db,
            filename=filename,
            content=content,
            vendor_name=vendor_name,
            vendor_website=(form.get("vendor_website") or ""),
        )
    except StockListValidationError as exc:
        raise HTTPException(exc.status_code, exc.message) from exc

    enrich_triggered = await _maybe_enrich_vendor(db, result)

    return {
        "imported_rows": result.imported_rows,
        "skipped_rows": result.skipped_rows,
        "total_rows": result.total_rows,
        "vendor_name": result.vendor_name,
        "enrich_triggered": enrich_triggered,
        "warnings": result.warnings,
    }


async def _maybe_enrich_vendor(db: Session, result: StockListResult) -> bool:
    """Fire background vendor enrichment when the ingest flagged a brand-new vendor with
    a domain and an enrichment credential is configured.

    Kept in the router (not the shared service) because background-task wiring + the
    credential gate are HTTP-side-effect concerns; the service stays pure/sync.
    """
    if not result.enrich_vendor or result.vendor_card_id is None:
        return False
    if not (
        get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
        or get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
    ):
        return False
    vendor_card = db.get(VendorCard, result.vendor_card_id)
    if not vendor_card or not vendor_card.domain:
        return False
    await safe_background_task(
        _background_enrich_vendor(vendor_card.id, vendor_card.domain, vendor_card.display_name),
        task_name="enrich_vendor_bg",
    )
    return True

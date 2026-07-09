"""routers/materials.py — Material Card CRUD, enrichment, merge, and stock import.

Handles material card listing, detail, update, enrichment, soft-delete/restore,
merge operations, and standalone stock list import.

Called by: main.py (router mount)
Depends on: models, dependencies, stock_list_ingest, cache, normalization, audit_service
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
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
    merge_material_cards as _merge_material_cards_service,
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
    now_iso = datetime.now(timezone.utc).isoformat()
    prov = dict(card.enrichment_provenance or {})
    for field in fields:
        prov[field] = {"source": "manual", "tier": 100, "confidence": 1.0, "fetched_at": now_iso}
    card.enrichment_provenance = prov


def _actor_email(user: User) -> str:
    """Audit/merge actor label — the user's email, or ``"admin"`` if it's unset."""
    return user.email if hasattr(user, "email") else "admin"


def _backfill_manufacturer(card: MaterialCard, db: Session) -> None:
    """Fill a card's missing manufacturer from its MPN prefix, committing on a hit.

    Shared by the two single-card read endpoints (by-id and by-mpn) so a card surfaced
    without a manufacturer gets one inferred lazily on first view.
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
        card.enrich_requested_at = datetime.now(timezone.utc)
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
    except (ValueError, TypeError):
        raise HTTPException(400, "limit and offset must be integers")

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


@router.get("/api/materials/by-mpn/{mpn}")
async def get_material_by_mpn(mpn: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Look up a material card by MPN."""
    norm = normalize_mpn_key(mpn)
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm).filter(MaterialCard.deleted_at.is_(None)).first()
    if not card:
        raise HTTPException(404, "No material card found for this MPN")
    _backfill_manufacturer(card, db)
    return material_card_to_dict(card, db)


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


@router.post("/api/materials/{card_id}/enrich")
async def enrich_material(
    card_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Apply AI-generated enrichment data to a material card.

    ``category``/``manufacturer`` are PROVENANCED columns and go through the F1 ladder
    (never raw setattr): the body's ``source`` is honored only when it is a registered
    SOURCE_TIER key BELOW the ground-truth band (< trio_source/95). ``manual`` (100) is
    a human assertion and ``trio_source`` (95) is TRIO's own part master — a pusher
    claiming either could overwrite a genuine human edit, permanently lock the column
    against every future enrichment correction, and corrupt the validation-conflict
    contract (``record_validation_conflict`` treats ``manual`` as a human value).
    Anything unregistered (the default ``claude_agent``) or ground-truth-tier is
    DEMOTED to ``ai_guess`` (tier 40) — logged at WARNING, and reported as
    ``ladder_source`` in the response — so un-vouched external AI data fills empty
    columns but never displaces decode / vendor / trio / manual provenance. Off-vocab
    categories are rejected, never persisted. ``updated_fields`` reports only writes
    that actually landed; a category/manufacturer write the ladder (or the category
    normalizer) refused is listed in ``rejected_fields``.
    """
    from ..services.spec_tiers import SOURCE_TIER, set_category, set_manufacturer

    card = db.get(MaterialCard, card_id)
    if not card:
        raise HTTPException(404, "Material not found")
    body = await request.json()
    source = body.get("source", "claude_agent")
    if source in SOURCE_TIER and SOURCE_TIER[source] < SOURCE_TIER["trio_source"]:
        ladder_source = source
    else:
        ladder_source = "ai_guess"
        if source != "claude_agent":
            # Loud demotion: ai_guess IS registered, so tier_for's unknown-source
            # WARNING never fires for it — without this line a pusher sending
            # "digikey" instead of "digikey_api" (or claiming "manual") would lose
            # silently forever with nothing to find in the logs.
            logger.warning(
                "enrich_material: card={} body source {!r} demoted to ai_guess/40 ({})",
                card_id,
                source,
                "not a registered SOURCE_TIER key"
                if source not in SOURCE_TIER
                else "ground-truth tiers are not honored from pushers",
            )
    raw_confidence = body.get("confidence")
    if raw_confidence is None:
        confidence = 0.5
    else:
        try:
            confidence = min(max(float(raw_confidence), 0.0), 1.0)
        except (TypeError, ValueError):
            raise HTTPException(422, f'"confidence" must be a number between 0 and 1, got {raw_confidence!r}.')
    enrichment_fields = (
        "lifecycle_status",
        "package_type",
        "rohs_status",
        "pin_count",
        "datasheet_url",
        "cross_references",
        "specs_summary",
        "description",
    )
    updated = []
    rejected = []
    for field in enrichment_fields:
        val = body.get(field)
        if val is not None:
            setattr(card, field, val)
            updated.append(field)
    if body.get("category") is not None:
        if set_category(card, body["category"], ladder_source, confidence):
            updated.append("category")
        else:
            rejected.append("category")
    if body.get("manufacturer") is not None:
        if set_manufacturer(card, body["manufacturer"], ladder_source, confidence):
            updated.append("manufacturer")
        else:
            rejected.append("manufacturer")
    if updated:
        card.enrichment_source = source
        card.enriched_at = datetime.now(timezone.utc)
    db.commit()
    invalidate_prefix("material_list")
    return {
        "ok": True,
        "updated_fields": updated,
        "rejected_fields": rejected,
        "ladder_source": ladder_source,
        "card_id": card_id,
    }


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
    card.deleted_at = datetime.now(timezone.utc)
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


@router.post("/api/materials/{card_id}/restore")
async def restore_material(card_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Restore a soft-deleted material card."""
    from ..services.audit_service import log_audit

    card = db.get(MaterialCard, card_id)
    if not card:
        raise HTTPException(404, "Material not found")
    if card.deleted_at is None:
        raise HTTPException(400, "Card is not deleted")
    card.deleted_at = None
    log_audit(
        db,
        material_card_id=card.id,
        action="restored",
        normalized_mpn=card.normalized_mpn,
        created_by=_actor_email(user),
    )
    db.commit()
    invalidate_prefix("material_list")
    return {"ok": True}


# -- Material Card Merge -------------------------------------------------------
@router.post("/api/materials/merge")
async def merge_material_cards(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Merge two material cards: move all linked records from source to target, then delete source."""
    body = await request.json()
    source_id = body.get("source_card_id")
    target_id = body.get("target_card_id")
    if not source_id or not target_id:
        raise HTTPException(400, "source_card_id and target_card_id are required")

    try:
        result = _merge_material_cards_service(db, source_id, target_id, _actor_email(user))
    except ValueError as e:
        raise HTTPException(400 if "itself" in str(e) else 404, str(e))

    db.commit()
    invalidate_prefix("material_list")
    return result


# -- Admin: Backfill Missing Manufacturers ------------------------------------


@router.post("/materials/backfill-manufacturers", tags=["admin"])
async def backfill_manufacturers(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """One-time admin endpoint to enrich all material cards missing a manufacturer via
    prefix-match."""
    count = backfill_missing_manufacturers(db)
    db.commit()
    invalidate_prefix("material_list")
    return {"enriched_records": count}


# -- Part Number Import -------------------------------------------------------


@router.post("/api/materials/import-part-numbers")
async def import_part_numbers(request: Request, user: User = Depends(require_buyer), db: Session = Depends(get_db)):
    """Import a bare list of part numbers (one MPN per row) as MaterialCards.

    Accepts CSV/XLSX/TSV and HTML-table-as-.xls. Creates bare cards
    (enrichment_status='unenriched'); enrichment runs separately.
    """
    import os as _os

    from ..file_utils import extract_mpns_with_rows, parse_tabular_file
    from ..search_service import resolve_material_card, run_deterministic_passes
    from ..utils.normalization import normalize_mpn

    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "No file uploaded")
    ext = _os.path.splitext(file.filename or "")[1].lower()
    if ext not in {".csv", ".xlsx", ".xls", ".tsv"}:
        raise HTTPException(400, f"Invalid file type '{ext}'")
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large -- 10MB maximum")

    rows = parse_tabular_file(content, file.filename or "")
    mpn_rows = extract_mpns_with_rows(rows)
    if not mpn_rows:
        raise HTTPException(400, "No part numbers found in file")

    created = existing = skipped = 0
    card_ids: list[int] = []
    warnings: list[dict] = []
    # row numbers are 1-based SOURCE-file rows (header = row 1) so a warning's `row`
    # points at the spreadsheet line the user can actually open and fix.
    for row_no, mpn in mpn_rows:
        # V3 normalization gate — never silent: surface the row + reason. normalize_mpn
        # (not the dedup key) owns the >=3-chars rule.
        if not normalize_mpn(mpn):
            skipped += 1
            warnings.append({"row": row_no, "field": "mpn", "reason": f"invalid MPN {mpn!r} (min 3 chars)"})
            continue
        card = resolve_material_card(mpn, db)
        if card is None:
            skipped += 1
            warnings.append({"row": row_no, "field": "mpn", "reason": f"could not create card for {mpn!r}"})
            continue
        card_ids.append(card.id)
        # resolve_material_card logs created vs resolved; detect new by enrichment_status default
        if card.enrichment_status == "unenriched" and card.enriched_at is None and card.search_count == 0:
            created += 1
        else:
            existing += 1
    # Inline deterministic passes over every touched card — same session, committed
    # together. NO enrich_requested_at stamp: bulk imports ride the created_at fast
    # lane (a 1,800-row import must not monopolize the worker's priority lane).
    run_deterministic_passes(db, card_ids)
    db.commit()
    return {
        "created": created,
        "existing": existing,
        "skipped": skipped,
        "total_rows": len(mpn_rows),
        "warnings": warnings,
    }


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

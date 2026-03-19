"""routers/materials.py — Material Card CRUD, enrichment, merge, and stock import.

Handles material card listing, detail, update, enrichment, soft-delete/restore,
merge operations, and standalone stock list import.

Called by: main.py (router mount)
Depends on: models, dependencies, vendor_helpers, cache, normalization, audit_service
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func as sqlfunc
from sqlalchemy.exc import IntegrityError
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
from ..services.price_snapshot_service import record_price_snapshot
from ..utils.async_helpers import safe_background_task
from ..utils.normalization import normalize_mpn_key
from ..utils.sql_helpers import escape_like
from ..utils.vendor_helpers import _background_enrich_vendor
from ..vendor_utils import normalize_vendor_name

router = APIRouter(tags=["vendors"])


# -- Material Card CRUD -------------------------------------------------------


@router.get("/api/materials")
async def list_materials(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    q = request.query_params.get("q", "").strip().lower()
    limit = min(int(request.query_params.get("limit", "200")), 1000)
    offset = max(int(request.query_params.get("offset", "0")), 0)

    @cached_endpoint(prefix="material_list", ttl_hours=2, key_params=["q", "limit", "offset"])
    def _fetch(q, limit, offset, user, db):
        query = (
            db.query(MaterialCard)
            .filter(MaterialCard.deleted_at.is_(None))
            .order_by(MaterialCard.last_searched_at.desc())
        )
        if q:
            # TODO: Add minimum length validation (e.g., 2 chars) to prevent broad queries
            safe_q = escape_like(q)
            query = query.filter(MaterialCard.normalized_mpn.ilike(f"{safe_q}%"))
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
    if not card.manufacturer:
        inferred = _infer_manufacturer_from_prefix(db, card.normalized_mpn)
        if inferred:
            card.manufacturer = inferred
            db.add(card)
            db.commit()
            invalidate_prefix("material_list")
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
    if not card.manufacturer:
        inferred = _infer_manufacturer_from_prefix(db, card.normalized_mpn)
        if inferred:
            card.manufacturer = inferred
            db.add(card)
            db.commit()
            invalidate_prefix("material_list")
    return material_card_to_dict(card, db)


@router.put("/api/materials/{card_id}")
async def update_material(
    card_id: int,
    data: MaterialCardUpdate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    card = db.get(MaterialCard, card_id)
    if not card or card.deleted_at is not None:
        raise HTTPException(404, "Material not found")
    if data.manufacturer is not None:
        card.manufacturer = data.manufacturer
    if data.description is not None:
        card.description = data.description
    if data.display_mpn is not None and data.display_mpn.strip():
        card.display_mpn = data.display_mpn.strip()
    # Enrichment fields
    for field in (
        "lifecycle_status",
        "package_type",
        "category",
        "rohs_status",
        "pin_count",
        "datasheet_url",
        "cross_references",
        "specs_summary",
    ):
        val = getattr(data, field, None)
        if val is not None:
            setattr(card, field, val)
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
    """Apply AI-generated enrichment data to a material card."""
    card = db.get(MaterialCard, card_id)
    if not card:
        raise HTTPException(404, "Material not found")
    body = await request.json()
    enrichment_fields = (
        "lifecycle_status",
        "package_type",
        "category",
        "rohs_status",
        "pin_count",
        "datasheet_url",
        "cross_references",
        "specs_summary",
        "manufacturer",
        "description",
    )
    updated = []
    for field in enrichment_fields:
        val = body.get(field)
        if val is not None:
            setattr(card, field, val)
            updated.append(field)
    if updated:
        card.enrichment_source = body.get("source", "gradient_agent")
        card.enriched_at = datetime.now(timezone.utc)
    db.commit()
    invalidate_prefix("material_list")
    return {"ok": True, "updated_fields": updated, "card_id": card_id}


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
        created_by=user.email if hasattr(user, "email") else "admin",
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
        created_by=user.email if hasattr(user, "email") else "admin",
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
        result = _merge_material_cards_service(
            db, source_id, target_id, user.email if hasattr(user, "email") else "admin"
        )
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


# -- Standalone Stock Import ---------------------------------------------------


@router.post("/api/materials/import-stock")
async def import_stock_list_standalone(
    request: Request, user: User = Depends(require_buyer), db: Session = Depends(get_db)
):
    """Import a vendor stock list -- stores ALL rows as MaterialCard +
    MaterialVendorHistory."""
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(400, "No file uploaded")

    # Validate file type
    import os as _os

    ext = _os.path.splitext(file.filename or "")[1].lower()
    allowed_extensions = {".csv", ".xlsx", ".xls", ".tsv"}
    if ext not in allowed_extensions:
        raise HTTPException(400, f"Invalid file type '{ext}'. Allowed: {', '.join(sorted(allowed_extensions))}")

    # Sanitize vendor name — strip HTML before length check
    import re as _re

    vendor_name = (form.get("vendor_name") or "").strip()
    vendor_name = _re.sub(r"<[^>]+>", "", vendor_name).strip()
    if not vendor_name:
        raise HTTPException(400, "Vendor name is required")
    if len(vendor_name) > 255:
        raise HTTPException(400, "Vendor name must be 255 characters or fewer")

    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large -- 10MB maximum")

    from ..file_utils import normalize_stock_row, parse_tabular_file

    rows = parse_tabular_file(content, file.filename or "upload.csv")

    # Upsert VendorCard
    vendor_website = (form.get("vendor_website") or "").strip()
    norm_vendor = normalize_vendor_name(vendor_name)
    vendor_card = db.query(VendorCard).filter_by(normalized_name=norm_vendor).first()
    new_vendor = False
    if not vendor_card:
        domain = ""
        if vendor_website:
            domain = (
                vendor_website.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0].lower()
            )
        vendor_card = VendorCard(
            normalized_name=norm_vendor,
            display_name=vendor_name,
            domain=domain or None,
            emails=[],
            phones=[],
        )
        db.add(vendor_card)
        try:
            db.flush()
            new_vendor = True
        except IntegrityError:
            db.rollback()
            vendor_card = db.query(VendorCard).filter_by(normalized_name=norm_vendor).first()

    imported = 0
    skipped = 0

    for raw_row in rows:
        parsed = normalize_stock_row(raw_row)
        if not parsed:
            skipped += 1
            continue

        norm = normalize_mpn_key(parsed["mpn"])
        if not norm:
            skipped += 1
            continue

        # Upsert MaterialCard
        card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
        if not card:
            card = MaterialCard(
                normalized_mpn=norm,
                display_mpn=parsed["mpn"].strip(),
                manufacturer=parsed.get("manufacturer") or "",
            )
            db.add(card)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()

        # Upsert MaterialVendorHistory
        mvh = db.query(MaterialVendorHistory).filter_by(material_card_id=card.id, vendor_name=norm_vendor).first()
        if mvh:
            mvh.last_seen = datetime.now(timezone.utc)
            mvh.times_seen = (mvh.times_seen or 0) + 1
            if parsed.get("qty") is not None:
                mvh.last_qty = parsed["qty"]
            if parsed.get("price") is not None:
                mvh.last_price = parsed["price"]
                record_price_snapshot(
                    db=db,
                    material_card_id=card.id,
                    vendor_name=norm_vendor,
                    price=parsed.get("price"),
                    source="stock_list",
                )
            if parsed.get("manufacturer"):
                mvh.last_manufacturer = parsed["manufacturer"]
            mvh.source_type = "stock_list"
        else:
            mvh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name=norm_vendor,
                vendor_name_normalized=norm_vendor,
                source_type="stock_list",
                source="stock_list",
                last_qty=parsed.get("qty"),
                last_price=parsed.get("price"),
                last_manufacturer=parsed.get("manufacturer") or "",
            )
            db.add(mvh)
            record_price_snapshot(
                db=db, material_card_id=card.id, vendor_name=norm_vendor, price=parsed.get("price"), source="stock_list"
            )

        imported += 1

    vendor_card.sighting_count = (vendor_card.sighting_count or 0) + imported
    db.commit()
    invalidate_prefix("material_list")

    # Trigger enrichment for new vendor with domain
    enrich_triggered = False
    if new_vendor and vendor_card.domain and not vendor_card.last_enriched_at:
        if get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") or get_credential_cached(
            "anthropic_ai", "ANTHROPIC_API_KEY"
        ):
            await safe_background_task(
                _background_enrich_vendor(vendor_card.id, vendor_card.domain, vendor_card.display_name),
                task_name="enrich_vendor_bg",
            )
            enrich_triggered = True

    return {
        "imported_rows": imported,
        "skipped_rows": skipped,
        "total_rows": len(rows),
        "vendor_name": vendor_name,
        "enrich_triggered": enrich_triggered,
    }

"""
routers/materials.py — Material Card CRUD, enrichment, merge, and stock import.

Handles material card listing, detail, update, enrichment, soft-delete/restore,
merge operations, and standalone stock list import.

Called by: main.py (router mount)
Depends on: models, dependencies, vendor_helpers, cache, normalization, audit_service
"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..cache.decorators import cached_endpoint
from ..database import get_db
from ..dependencies import require_admin, require_buyer, require_user
from ..models import (
    MaterialCard,
    MaterialVendorHistory,
    Offer,
    Requirement,
    Sighting,
    User,
    VendorCard,
)
from ..schemas.vendors import MaterialCardUpdate
from ..services.credential_service import get_credential_cached
from ..utils.normalization import normalize_mpn_key
from ..utils.sql_helpers import escape_like
from ..utils.vendor_helpers import _background_enrich_vendor
from ..vendor_utils import normalize_vendor_name

router = APIRouter(tags=["vendors"])


# -- Manufacturer Enrichment Helpers ------------------------------------------


def _infer_manufacturer_from_prefix(db: Session, mpn: str) -> str | None:
    """Walk from longest to shortest prefix to find a known manufacturer."""
    for length in range(len(mpn) - 1, 6, -1):  # minimum 7-char prefix
        prefix = mpn[:length]
        match = (
            db.query(MaterialCard)
            .filter(
                MaterialCard.normalized_mpn == prefix,
                MaterialCard.manufacturer.isnot(None),
                MaterialCard.manufacturer != "",
            )
            .first()
        )
        if match:
            return match.manufacturer
    return None


def backfill_missing_manufacturers(db: Session) -> int:
    """Bulk-update all rows where manufacturer IS NULL/empty and a prefix-match donor exists."""
    null_parts = (
        db.query(MaterialCard)
        .filter(
            (MaterialCard.manufacturer.is_(None)) | (MaterialCard.manufacturer == "")
        )
        .all()
    )
    updated = 0
    for part in null_parts:
        inferred = _infer_manufacturer_from_prefix(db, part.normalized_mpn)
        if inferred:
            part.manufacturer = inferred
            db.add(part)
            updated += 1
    db.commit()
    return updated


# -- Material Card Serialization -----------------------------------------------


def material_card_to_dict(card: MaterialCard, db: Session) -> dict:
    """Serialize a material card with vendor history, sightings, and offers."""
    history = (
        db.query(MaterialVendorHistory)
        .filter_by(material_card_id=card.id)
        .order_by(MaterialVendorHistory.last_seen.desc())
        .all()
    )

    # Find sightings and offers for this material card via FK
    sightings_list = []
    offers_list = []

    sightings = (
        db.query(Sighting)
        .filter(Sighting.material_card_id == card.id)
        .order_by(Sighting.created_at.desc())
        .limit(50)
        .all()
    )
    sightings_list = [
        {
            "id": s.id,
            "vendor_name": s.vendor_name,
            "qty_available": s.qty_available,
            "unit_price": s.unit_price,
            "currency": s.currency or "USD",
            "source_type": s.source_type,
            "is_authorized": s.is_authorized,
            "date_code": s.date_code,
            "condition": s.condition,
            "lead_time": s.lead_time,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in sightings
        if not s.is_unavailable
    ]

    # Tags (brand + commodity)
    from ..models.tags import MaterialTag, Tag

    tag_rows = (
        db.query(Tag.name, Tag.tag_type, MaterialTag.confidence, MaterialTag.source)
        .join(MaterialTag, MaterialTag.tag_id == Tag.id)
        .filter(MaterialTag.material_card_id == card.id, MaterialTag.confidence >= 0.70)
        .order_by(MaterialTag.confidence.desc())
        .all()
    )
    tags_list = [
        {"name": name, "type": tt, "confidence": round(float(conf), 2), "source": src}
        for name, tt, conf, src in tag_rows
    ]

    offers = db.query(Offer).filter(Offer.material_card_id == card.id).order_by(Offer.created_at.desc()).limit(50).all()
    offers_list = [
        {
            "id": o.id,
            "vendor_name": o.vendor_name,
            "qty_available": o.qty_available,
            "unit_price": float(o.unit_price) if o.unit_price else None,
            "currency": o.currency or "USD",
            "lead_time": o.lead_time,
            "date_code": o.date_code,
            "condition": o.condition,
            "status": o.status,
            "source": o.source,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o in offers
    ]

    return {
        "id": card.id,
        "normalized_mpn": card.normalized_mpn,
        "display_mpn": card.display_mpn,
        "manufacturer": card.manufacturer,
        "description": card.description,
        "search_count": card.search_count or 0,
        "last_searched_at": card.last_searched_at.isoformat() if card.last_searched_at else None,
        "vendor_count": len(history),
        "vendor_history": [
            {
                "id": vh.id,
                "vendor_name": vh.vendor_name,
                "source_type": vh.source_type,
                "is_authorized": vh.is_authorized,
                "first_seen": vh.first_seen.isoformat() if vh.first_seen else None,
                "last_seen": vh.last_seen.isoformat() if vh.last_seen else None,
                "times_seen": vh.times_seen or 1,
                "last_qty": vh.last_qty,
                "last_price": vh.last_price,
                "last_currency": vh.last_currency,
                "last_manufacturer": vh.last_manufacturer,
                "vendor_sku": vh.vendor_sku,
            }
            for vh in history
        ],
        "sightings": sightings_list,
        "offers": offers_list,
        "tags": tags_list,
        # Enrichment fields
        "lifecycle_status": card.lifecycle_status,
        "package_type": card.package_type,
        "category": card.category,
        "rohs_status": card.rohs_status,
        "pin_count": card.pin_count,
        "datasheet_url": card.datasheet_url,
        "cross_references": card.cross_references or [],
        "specs_summary": card.specs_summary,
        "enrichment_source": card.enrichment_source,
        "enriched_at": card.enriched_at.isoformat() if card.enriched_at else None,
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


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
    return material_card_to_dict(card, db)


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
    return {"ok": True, "updated_fields": updated, "card_id": card_id}


@router.delete("/api/materials/{card_id}")
async def delete_material(card_id: int, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Soft-delete a material card. Sets deleted_at timestamp; records are preserved."""
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
    return {"ok": True}


# -- Material Card Merge -------------------------------------------------------


@router.post("/api/materials/merge")
async def merge_material_cards(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Merge two material cards: move all linked records from source to target, then delete source.

    Body: {"source_card_id": int, "target_card_id": int}

    All requirements, sightings, and offers pointing to source_card_id are re-pointed
    to target_card_id.  Vendor histories are merged (combine counts, keep earliest
    first_seen, latest last_seen).  The source card is deleted after merge.
    """
    body = await request.json()
    source_id = body.get("source_card_id")
    target_id = body.get("target_card_id")
    if not source_id or not target_id:
        raise HTTPException(400, "source_card_id and target_card_id are required")
    if source_id == target_id:
        raise HTTPException(400, "Cannot merge a card with itself")

    source = db.get(MaterialCard, source_id)
    target = db.get(MaterialCard, target_id)
    if not source:
        raise HTTPException(404, f"Source card {source_id} not found")
    if not target:
        raise HTTPException(404, f"Target card {target_id} not found")

    # 1. Re-point requirements, sightings, offers
    reassigned = {}
    for model, name in [(Requirement, "requirements"), (Sighting, "sightings"), (Offer, "offers")]:
        count = (
            db.query(model)
            .filter(model.material_card_id == source_id)
            .update({model.material_card_id: target_id}, synchronize_session="fetch")
        )
        reassigned[name] = count

    # 2. Merge vendor histories
    source_vhs = db.query(MaterialVendorHistory).filter_by(material_card_id=source_id).all()
    target_vhs = {
        normalize_vendor_name(vh.vendor_name): vh
        for vh in db.query(MaterialVendorHistory).filter_by(material_card_id=target_id).all()
    }

    vh_merged = 0
    vh_moved = 0
    for svh in source_vhs:  # pragma: no cover
        vn_key = normalize_vendor_name(svh.vendor_name)
        tvh = target_vhs.get(vn_key)
        if tvh:
            # Merge into existing target record
            tvh.times_seen = (tvh.times_seen or 1) + (svh.times_seen or 1)
            if svh.first_seen and (not tvh.first_seen or svh.first_seen < tvh.first_seen):
                tvh.first_seen = svh.first_seen
            if svh.last_seen and (not tvh.last_seen or svh.last_seen > tvh.last_seen):
                tvh.last_seen = svh.last_seen
                # Update "last" fields from the more recent record
                if svh.last_qty is not None:
                    tvh.last_qty = svh.last_qty
                if svh.last_price is not None:
                    tvh.last_price = svh.last_price
                if svh.last_currency:
                    tvh.last_currency = svh.last_currency
                if svh.last_manufacturer:
                    tvh.last_manufacturer = svh.last_manufacturer
                if svh.vendor_sku:
                    tvh.vendor_sku = svh.vendor_sku
            if svh.is_authorized:
                tvh.is_authorized = True
            db.delete(svh)
            vh_merged += 1
        else:
            # Move to target card
            svh.material_card_id = target_id
            vh_moved += 1

    # 3. Merge card metadata
    target.search_count = (target.search_count or 0) + (source.search_count or 0)
    if not target.manufacturer and source.manufacturer:  # pragma: no cover
        target.manufacturer = source.manufacturer
    if not target.description and source.description:  # pragma: no cover
        target.description = source.description
    # Enrichment: keep target's if present, else take source's
    for field in (
        "lifecycle_status",
        "package_type",
        "category",
        "rohs_status",
        "pin_count",
        "datasheet_url",
        "specs_summary",
    ):
        if getattr(target, field) is None and getattr(source, field) is not None:  # pragma: no cover
            setattr(target, field, getattr(source, field))

    # 4. Audit log
    from ..services.audit_service import log_audit

    log_audit(
        db,
        material_card_id=target_id,
        action="merged",
        old_card_id=source_id,
        new_card_id=target_id,
        normalized_mpn=target.normalized_mpn,
        details={
            "source_mpn": source.normalized_mpn,
            "reassigned": reassigned,
            "vh_merged": vh_merged,
            "vh_moved": vh_moved,
        },
        created_by=user.email if hasattr(user, "email") else "admin",
    )

    # 5. Delete source card
    db.delete(source)
    db.commit()

    logger.info(
        "MC_METRIC: action=merged source_id=%d target_id=%d source_mpn=%s target_mpn=%s "
        "reassigned=%s vh_merged=%d vh_moved=%d",
        source_id,
        target_id,
        source.normalized_mpn,
        target.normalized_mpn,
        reassigned,
        vh_merged,
        vh_moved,
    )
    return {
        "ok": True,
        "target_card_id": target_id,
        "source_card_id": source_id,
        "reassigned": reassigned,
        "vendor_histories_merged": vh_merged,
        "vendor_histories_moved": vh_moved,
    }


# -- Admin: Backfill Missing Manufacturers ------------------------------------


@router.post("/materials/backfill-manufacturers", tags=["admin"])
async def backfill_manufacturers(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    """One-time admin endpoint to enrich all material cards missing a manufacturer via prefix-match."""
    count = backfill_missing_manufacturers(db)
    return {"enriched_records": count}


# -- Standalone Stock Import ---------------------------------------------------


@router.post("/api/materials/import-stock")
async def import_stock_list_standalone(
    request: Request, user: User = Depends(require_buyer), db: Session = Depends(get_db)
):
    """Import a vendor stock list -- stores ALL rows as MaterialCard + MaterialVendorHistory."""
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
    vendor_name = _re.sub(r'<[^>]+>', '', vendor_name).strip()
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

        imported += 1

    vendor_card.sighting_count = (vendor_card.sighting_count or 0) + imported
    db.commit()

    # Trigger enrichment for new vendor with domain
    enrich_triggered = False
    if new_vendor and vendor_card.domain and not vendor_card.last_enriched_at:
        if get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") or get_credential_cached(
            "anthropic_ai", "ANTHROPIC_API_KEY"
        ):
            asyncio.create_task(_background_enrich_vendor(vendor_card.id, vendor_card.domain, vendor_card.display_name))
            enrich_triggered = True

    return {
        "imported_rows": imported,
        "skipped_rows": skipped,
        "total_rows": len(rows),
        "vendor_name": vendor_name,
        "enrich_triggered": enrich_triggered,
    }

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ...config import settings
from ...database import get_db
from ...dependencies import require_buyer, require_user
from ...models import (
    ActivityLog,
    ChangeLog,
    Offer,
    OfferAttachment,
    Quote,
    Requirement,
    Requisition,
    User,
    VendorCard,
    VendorReview,
)
from ...schemas.crm import OfferCreate, OfferUpdate, OneDriveAttach
from ...schemas.responses import OfferListResponse
from ...services.credential_service import get_credential_cached
from ...utils.normalization import (
    normalize_condition,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
)
from ...vendor_utils import normalize_vendor_name
from ._helpers import record_changes, _preload_last_quoted_prices

router = APIRouter()


# ── Offers ───────────────────────────────────────────────────────────────


@router.get("/api/requisitions/{req_id}/offers", response_model=OfferListResponse, response_model_exclude_none=True)
async def list_offers(
    req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
):
    """List offers for a requisition grouped by requirement."""
    from ...dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    query = db.query(Offer).filter(Offer.requisition_id == req_id)
    # Hide draft/pending_review offers from buyers — only sales/admin/manager see them
    if user.role == "buyer":
        query = query.filter(Offer.status != "pending_review")
    offers = (
        query
        .options(
            joinedload(Offer.entered_by),
            joinedload(Offer.updated_by),
            selectinload(Offer.attachments),
        )
        .order_by(
            Offer.requirement_id,
            Offer.unit_price,
        )
        .all()
    )

    # Cross-reference quoted offers — collect offer_ids used in quotes
    quoted_map: dict[int, str] = {}  # offer_id → quote_number
    req_quotes = db.query(Quote).filter(
        Quote.requisition_id == req_id,
        Quote.status.in_(["draft", "sent", "won"]),
    ).all()
    for q in req_quotes:
        for li in (q.line_items or []):
            oid = li.get("offer_id")
            if oid:
                quoted_map[oid] = q.quote_number or f"Q-{q.id}"
    # Detect unseen offers before marking as viewed
    latest_offer_at = max((o.created_at for o in offers), default=None)
    has_new = bool(
        latest_offer_at
        and (not req.offers_viewed_at or latest_offer_at > req.offers_viewed_at)
    )
    # Mark as viewed if the requisition owner is viewing
    if offers and user.id == req.created_by:
        req.offers_viewed_at = datetime.now(timezone.utc)
        db.commit()
    # Batch-fetch vendor ratings for all vendor_card_ids
    card_ids = {o.vendor_card_id for o in offers if o.vendor_card_id}
    rating_map: dict[int, dict] = {}
    if card_ids:
        rating_rows = (
            db.query(
                VendorReview.vendor_card_id,
                sqlfunc.avg(VendorReview.rating),
                sqlfunc.count(VendorReview.id),
            )
            .filter(VendorReview.vendor_card_id.in_(card_ids))
            .group_by(VendorReview.vendor_card_id)
            .all()
        )
        rating_map = {
            r[0]: {"avg": round(float(r[1]), 1), "count": r[2]} for r in rating_rows
        }

    groups: dict[int, list] = {}
    for o in offers:
        key = o.requirement_id or 0
        if key not in groups:
            groups[key] = []
        atts = [
            {
                "id": a.id,
                "file_name": a.file_name,
                "onedrive_url": a.onedrive_url,
                "thumbnail_url": a.thumbnail_url,
                "content_type": a.content_type,
            }
            for a in (o.attachments or [])
        ]
        groups[key].append(
            {
                "id": o.id,
                "requirement_id": o.requirement_id,
                "vendor_name": o.vendor_name,
                "vendor_card_id": o.vendor_card_id,
                "mpn": o.mpn,
                "manufacturer": o.manufacturer,
                "qty_available": o.qty_available,
                "unit_price": float(o.unit_price) if o.unit_price else None,
                "lead_time": o.lead_time,
                "date_code": o.date_code,
                "condition": o.condition,
                "packaging": o.packaging,
                "firmware": o.firmware,
                "hardware_code": o.hardware_code,
                "moq": o.moq,
                "warranty": o.warranty,
                "country_of_origin": o.country_of_origin,
                "source": o.source,
                "status": o.status,
                "notes": o.notes,
                "entered_by": o.entered_by.name if o.entered_by else None,
                "entered_by_id": o.entered_by_id,
                "updated_by": o.updated_by.name if o.updated_by else None,
                "updated_at": o.updated_at.isoformat() if o.updated_at else None,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "quoted_on": quoted_map.get(o.id),
                "attachments": atts,
                "avg_rating": rating_map.get(o.vendor_card_id, {}).get("avg"),
                "review_count": rating_map.get(o.vendor_card_id, {}).get("count", 0),
            }
        )
    # Preload quoted prices ONCE instead of per-requirement DB call
    quoted_prices = _preload_last_quoted_prices(db)

    # ── Cross-requisition historical offers ──────────────────────────
    # Collect all MPNs (primary + substitutes) per requirement
    req_mpn_map: dict[int, set[str]] = {}  # requirement_id → set of normalized MPNs
    all_mpns: set[str] = set()
    primary_mpns: set[str] = set()
    for r in req.requirements:
        mpns: set[str] = set()
        p = (r.primary_mpn or "").upper().strip()
        if p:
            mpns.add(p)
            primary_mpns.add(p)
        for s in r.substitutes or []:
            s_norm = (s if isinstance(s, str) else "").upper().strip()
            if s_norm:
                mpns.add(s_norm)
        req_mpn_map[r.id] = mpns
        all_mpns |= mpns

    hist_by_req: dict[int, list] = {}
    if all_mpns:
        hist_query = (
            db.query(Offer)
            .filter(
                Offer.requisition_id != req_id,
                sqlfunc.upper(Offer.mpn).in_(all_mpns),
                Offer.status.in_(["active", "won"]),
            )
            .options(joinedload(Offer.entered_by))
            .order_by(Offer.created_at.desc())
            .limit(100)
            .all()
        )
        # Bucket historical offers into requirement groups
        for ho in hist_query:
            ho_mpn = (ho.mpn or "").upper().strip()
            for r in req.requirements:
                if ho_mpn in req_mpn_map.get(r.id, set()):
                    if r.id not in hist_by_req:
                        hist_by_req[r.id] = []
                    is_sub = ho_mpn not in primary_mpns or (
                        ho_mpn != (r.primary_mpn or "").upper().strip()
                    )
                    hist_by_req[r.id].append(
                        {
                            "id": ho.id,
                            "vendor_name": ho.vendor_name,
                            "vendor_card_id": ho.vendor_card_id,
                            "mpn": ho.mpn,
                            "manufacturer": ho.manufacturer,
                            "qty_available": ho.qty_available,
                            "unit_price": float(ho.unit_price) if ho.unit_price else None,
                            "lead_time": ho.lead_time,
                            "condition": ho.condition,
                            "source": ho.source,
                            "status": ho.status,
                            "notes": ho.notes,
                            "entered_by": ho.entered_by.name if ho.entered_by else None,
                            "created_at": ho.created_at.isoformat() if ho.created_at else None,
                            "from_requisition_id": ho.requisition_id,
                            "is_substitute": is_sub,
                        }
                    )
                    break  # assign to first matching requirement only

    result = []
    for r in req.requirements:
        target = float(r.target_price) if r.target_price else None
        last_q = quoted_prices.get((r.primary_mpn or "").upper().strip())
        result.append(
            {
                "requirement_id": r.id,
                "mpn": r.primary_mpn,
                "target_qty": r.target_qty,
                "target_price": target,
                "last_quoted": last_q,
                "offers": groups.get(r.id, []),
                "historical_offers": hist_by_req.get(r.id, []),
            }
        )
    return {
        "has_new_offers": has_new,
        "latest_offer_at": latest_offer_at.isoformat() if latest_offer_at else None,
        "groups": result,
    }


@router.post("/api/requisitions/{req_id}/offers")
async def create_offer(
    req_id: int,
    payload: OfferCreate,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ...dependencies import get_req_for_user

    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")

    card = None

    # 1) If frontend passed a vendor_card_id, use it directly
    if payload.vendor_card_id:
        card = db.get(VendorCard, payload.vendor_card_id)

    # 2) Exact match on normalized name
    if not card:
        norm_name = normalize_vendor_name(payload.vendor_name)
        card = db.query(VendorCard).filter(VendorCard.normalized_name == norm_name).first()

    # 3) Fuzzy match: ILIKE prefix search + fuzzy scoring
    if not card:
        from ...vendor_utils import fuzzy_match_vendor

        prefix = norm_name.split()[0] if norm_name else ""
        if prefix and len(prefix) >= 2:
            candidates = (
                db.query(VendorCard)
                .filter(VendorCard.normalized_name.ilike(f"{prefix}%"))
                .limit(20)
                .all()
            )
            if candidates:
                matches = fuzzy_match_vendor(
                    payload.vendor_name,
                    [c.display_name for c in candidates],
                    threshold=88,
                )
                if matches:
                    best_name = matches[0]["name"]
                    card = next(c for c in candidates if c.display_name == best_name)
                    # Append submitted name as alternate for future exact lookups
                    alts = list(card.alternate_names or [])
                    if payload.vendor_name not in alts and payload.vendor_name != card.display_name:
                        alts.append(payload.vendor_name)
                        card.alternate_names = alts

    # 4) No match — create new card
    if not card:
        domain = ""
        if payload.vendor_website:
            domain = (
                payload.vendor_website.replace("https://", "")
                .replace("http://", "")
                .replace("www.", "")
                .split("/")[0]
                .lower()
            )
        card = VendorCard(
            normalized_name=norm_name,
            display_name=payload.vendor_name,
            domain=domain or None,
            emails=[],
            phones=[],
        )
        db.add(card)
        db.flush()
        if domain and (
            get_credential_cached("clay_enrichment", "CLAY_API_KEY")
            or get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
            or get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
        ):
            from ...routers.vendors import _background_enrich_vendor

            asyncio.create_task(
                _background_enrich_vendor(card.id, domain, card.display_name)
            )
    offer = Offer(
        requisition_id=req_id,
        requirement_id=payload.requirement_id,
        vendor_card_id=card.id,
        vendor_name=card.display_name,
        mpn=payload.mpn,
        manufacturer=payload.manufacturer,
        qty_available=payload.qty_available,
        unit_price=payload.unit_price,
        lead_time=payload.lead_time,
        date_code=payload.date_code,
        condition=payload.condition,
        packaging=payload.packaging,
        firmware=payload.firmware,
        hardware_code=payload.hardware_code,
        moq=payload.moq,
        warranty=payload.warranty,
        country_of_origin=payload.country_of_origin,
        source=payload.source,
        vendor_response_id=payload.vendor_response_id,
        entered_by_id=user.id,
        notes=payload.notes,
        status=payload.status,
    )
    db.add(offer)
    old_status = req.status
    if req.status in ("active", "sourcing"):
        req.status = "offers"
    db.commit()

    # Teams: competitive quote alert if >20% below current best price
    try:
        if offer.unit_price and offer.unit_price > 0 and offer.requirement_id:
            from sqlalchemy import func
            best_price = (
                db.query(func.min(Offer.unit_price))
                .filter(
                    Offer.requirement_id == offer.requirement_id,
                    Offer.id != offer.id,
                    Offer.unit_price > 0,
                )
                .scalar()
            )
            if best_price and float(offer.unit_price) < float(best_price) * 0.8:
                pct = round((1 - float(offer.unit_price) / float(best_price)) * 100)
                from ...services.teams import send_competitive_quote_alert
                asyncio.create_task(send_competitive_quote_alert(
                    offer_id=offer.id,
                    mpn=offer.mpn,
                    vendor_name=offer.vendor_name,
                    offer_price=float(offer.unit_price),
                    best_price=float(best_price),
                    requisition_id=req_id,
                ))
                # In-app notification for requisition owner
                if req.created_by:
                    db.add(ActivityLog(
                        user_id=req.created_by,
                        activity_type="competitive_quote",
                        channel="system",
                        requisition_id=req_id,
                        contact_name=offer.vendor_name,
                        subject=f"Competitive quote: {offer.vendor_name} — {offer.mpn} at ${offer.unit_price} ({pct}% below best)",
                    ))
                    db.commit()
    except Exception:
        logger.debug("Activity event creation failed", exc_info=True)

    return {
        "id": offer.id,
        "vendor_name": offer.vendor_name,
        "vendor_card_id": offer.vendor_card_id,
        "mpn": offer.mpn,
        "req_status": req.status,
        "status_changed": req.status != old_status,
    }


@router.put("/api/offers/{offer_id}")
async def update_offer(
    offer_id: int,
    payload: OfferUpdate,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    changes = payload.model_dump(exclude_unset=True)
    # Snapshot old values for changelog
    trackable = [
        "vendor_name", "qty_available", "unit_price", "lead_time", "condition",
        "warranty", "manufacturer", "date_code", "packaging", "moq", "notes", "status",
    ]
    old_dict = {f: getattr(offer, f) for f in trackable}
    for field, value in changes.items():
        setattr(offer, field, value)
    new_dict = {f: getattr(offer, f) for f in trackable}
    record_changes(db, "offer", offer_id, user.id, old_dict, new_dict, trackable)
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    db.commit()
    return {"ok": True}


@router.delete("/api/offers/{offer_id}")
async def delete_offer(
    offer_id: int, user: User = Depends(require_buyer), db: Session = Depends(get_db)
):
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    db.delete(offer)
    db.commit()
    return {"ok": True}


@router.put("/api/offers/{offer_id}/reconfirm")
async def reconfirm_offer(
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark a historical offer as reconfirmed (still valid)."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    offer.reconfirmed_at = datetime.now(timezone.utc)
    offer.reconfirm_count = (offer.reconfirm_count or 0) + 1
    db.commit()
    return {
        "ok": True,
        "reconfirmed_at": offer.reconfirmed_at.isoformat(),
        "reconfirm_count": offer.reconfirm_count,
    }


@router.put("/api/offers/{offer_id}/approve")
async def approve_offer(
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Approve a pending_review offer → active."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.status != "pending_review":
        raise HTTPException(400, "Only pending_review offers can be approved")
    old_status = offer.status
    offer.status = "active"
    offer.approved_by_id = user.id
    offer.approved_at = datetime.now(timezone.utc)
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    record_changes(db, "offer", offer_id, user.id,
                   {"status": old_status}, {"status": "active"}, ["status"])
    db.commit()
    return {"ok": True, "status": "active"}


@router.put("/api/offers/{offer_id}/reject")
async def reject_offer(
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
    reason: str = "",
):
    """Reject a pending_review offer."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.status != "pending_review":
        raise HTTPException(400, "Only pending_review offers can be rejected")
    old_status = offer.status
    offer.status = "rejected"
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    if reason:
        offer.notes = f"{offer.notes or ''}\n[Rejected: {reason}]".strip()
    record_changes(db, "offer", offer_id, user.id,
                   {"status": old_status}, {"status": "rejected"}, ["status"])
    db.commit()
    return {"ok": True, "status": "rejected"}


@router.get("/api/changelog/{entity_type}/{entity_id}")
async def get_changelog(
    entity_type: str,
    entity_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Fetch change history for an entity."""
    if entity_type not in ("offer", "requirement", "requisition"):
        raise HTTPException(400, "Invalid entity_type")
    rows = (
        db.query(ChangeLog)
        .filter(ChangeLog.entity_type == entity_type, ChangeLog.entity_id == entity_id)
        .options(joinedload(ChangeLog.user))
        .order_by(ChangeLog.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "field_name": r.field_name,
            "old_value": r.old_value,
            "new_value": r.new_value,
            "user_name": r.user.name if r.user else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ── Offer Attachments (OneDrive) ─────────────────────────────────────────


@router.post("/api/offers/{offer_id}/attachments")
async def upload_offer_attachment(
    offer_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file to OneDrive and attach it to an offer."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10 MB)")
    # Upload to OneDrive: AvailAI/Offers/{req_id}/{filename}
    from ...utils.graph_client import GraphClient

    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected — please re-login")
    GraphClient(user.access_token)
    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    drive_path = (
        f"/me/drive/root:/AvailAI/Offers/{offer.requisition_id}/{safe_name}:/content"
    )
    from ...http_client import http

    resp = await http.put(
        f"https://graph.microsoft.com/v1.0{drive_path}",
        content=content,
        headers={
            "Authorization": f"Bearer {user.access_token}",
            "Content-Type": file.content_type or "application/octet-stream",
        },
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        logger.error(f"OneDrive upload failed: {resp.status_code} {resp.text[:300]}")
        raise HTTPException(502, "Failed to upload to OneDrive")
    result = resp.json()
    att = OfferAttachment(
        offer_id=offer_id,
        file_name=safe_name,
        onedrive_item_id=result.get("id"),
        onedrive_url=result.get("webUrl"),
        content_type=file.content_type,
        size_bytes=len(content),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    return {
        "id": att.id,
        "file_name": att.file_name,
        "onedrive_url": att.onedrive_url,
        "content_type": att.content_type,
    }


@router.post("/api/offers/{offer_id}/attachments/onedrive")
async def attach_from_onedrive(
    offer_id: int,
    body: OneDriveAttach,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Attach an existing OneDrive file to an offer by item ID."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    item_id = body.item_id
    from ...utils.graph_client import GraphClient

    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected")
    gc = GraphClient(user.access_token)
    item = await gc.get_json(f"/me/drive/items/{item_id}")
    if "error" in item:
        raise HTTPException(404, "OneDrive item not found")
    att = OfferAttachment(
        offer_id=offer_id,
        file_name=item.get("name", "file"),
        onedrive_item_id=item_id,
        onedrive_url=item.get("webUrl"),
        content_type=item.get("file", {}).get("mimeType"),
        size_bytes=item.get("size"),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    return {
        "id": att.id,
        "file_name": att.file_name,
        "onedrive_url": att.onedrive_url,
        "content_type": att.content_type,
    }


@router.delete("/api/offer-attachments/{att_id}")
async def delete_offer_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    att = db.get(OfferAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    # Delete from OneDrive if we have the item ID
    if att.onedrive_item_id and user.access_token:
        from ...utils.graph_client import GraphClient

        GraphClient(user.access_token)
        try:
            from ...http_client import http

            await http.delete(
                f"https://graph.microsoft.com/v1.0/me/drive/items/{att.onedrive_item_id}",
                headers={"Authorization": f"Bearer {user.access_token}"},
                timeout=15,
            )
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.warning(f"Failed to delete OneDrive item {att.onedrive_item_id}: {e}")
    db.delete(att)
    db.commit()
    return {"ok": True}


@router.get("/api/onedrive/browse")
async def browse_onedrive(
    path: str = "",
    user: User = Depends(require_user),
):
    """Browse user's OneDrive files for the picker."""
    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected")
    from ...utils.graph_client import GraphClient

    gc = GraphClient(user.access_token)
    if path:
        data = await gc.get_json(
            f"/me/drive/root:/{path}:/children",
            params={
                "$top": "50",
                "$select": "id,name,size,file,folder,webUrl,lastModifiedDateTime",
            },
        )
    else:
        data = await gc.get_json(
            "/me/drive/root/children",
            params={
                "$top": "50",
                "$select": "id,name,size,file,folder,webUrl,lastModifiedDateTime",
            },
        )
    if "error" in data:
        raise HTTPException(502, "Failed to browse OneDrive")
    items = data.get("value", [])
    return [
        {
            "id": i["id"],
            "name": i["name"],
            "is_folder": "folder" in i,
            "size": i.get("size"),
            "mime_type": i.get("file", {}).get("mimeType"),
            "web_url": i.get("webUrl"),
            "modified_at": i.get("lastModifiedDateTime"),
        }
        for i in items
    ]

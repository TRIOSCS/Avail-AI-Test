import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_buyer, require_user
from ...models import (
    ActivityLog,
    ChangeLog,
    MaterialCard,
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
from ...utils.normalization import normalize_mpn_key
from ...utils.sanitize import sanitize_text
from ...vendor_utils import normalize_vendor_name
from ._helpers import _preload_last_quoted_prices, record_changes

router = APIRouter()


# ── Offers ───────────────────────────────────────────────────────────────


@router.get("/api/requisitions/{req_id}/offers", response_model=OfferListResponse, response_model_exclude_none=True)
async def list_offers(req_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
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
        query.options(
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
    req_quotes = (
        db.query(Quote)
        .filter(
            Quote.requisition_id == req_id,
            Quote.status.in_(["draft", "sent", "won"]),
        )
        .all()
    )
    for q in req_quotes:
        for li in q.line_items or []:
            oid = li.get("offer_id")
            if oid:
                quoted_map[oid] = q.quote_number or f"Q-{q.id}"
    # Detect unseen offers before marking as viewed
    latest_offer_at = max((o.created_at for o in offers), default=None)
    has_new = bool(latest_offer_at and (not req.offers_viewed_at or latest_offer_at > req.offers_viewed_at))
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
        rating_map = {r[0]: {"avg": round(float(r[1]), 1), "count": r[2]} for r in rating_rows}

    # Batch-fetch parse confidence from vendor_responses for email-parsed offers
    vr_ids = {o.vendor_response_id for o in offers if o.vendor_response_id}
    conf_map: dict[int, int | None] = {}
    if vr_ids:
        from ...models import VendorResponse

        vr_rows = db.query(VendorResponse.id, VendorResponse.confidence).filter(VendorResponse.id.in_(vr_ids)).all()
        conf_map = {vr.id: round(vr.confidence * 100) if vr.confidence is not None else None for vr in vr_rows}

    # Batch-fetch risk flags for offers in this requisition
    from ...models.risk_flag import RiskFlag

    risk_rows = (
        db.query(RiskFlag)
        .filter(RiskFlag.requisition_id == req_id, RiskFlag.source_offer_id.isnot(None))
        .all()
    )
    risk_by_offer: dict[int, list] = {}
    for rf in risk_rows:
        if rf.source_offer_id not in risk_by_offer:
            risk_by_offer[rf.source_offer_id] = []
        risk_by_offer[rf.source_offer_id].append({
            "type": rf.type,
            "severity": rf.severity,
            "message": rf.message,
        })

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
                "valid_until": o.valid_until.isoformat() if o.valid_until else None,
                "currency": o.currency or "USD",
                "selected_for_quote": o.selected_for_quote or False,
                "selected_at": o.selected_at.isoformat() if o.selected_at else None,
                "entered_by": o.entered_by.name if o.entered_by else None,
                "entered_by_id": o.entered_by_id,
                "updated_by": o.updated_by.name if o.updated_by else None,
                "updated_at": o.updated_at.isoformat() if o.updated_at else None,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "quoted_on": quoted_map.get(o.id),
                "attachments": atts,
                "avg_rating": rating_map.get(o.vendor_card_id, {}).get("avg"),
                "review_count": rating_map.get(o.vendor_card_id, {}).get("count", 0),
                "parse_confidence": conf_map.get(o.vendor_response_id),
                "risk_flags": risk_by_offer.get(o.id, []),
            }
        )
    # Preload quoted prices ONCE instead of per-requirement DB call
    quoted_prices = _preload_last_quoted_prices(db)

    # ── Cross-requisition historical offers (via material_card_id FK) ──
    req_card_map: dict[int, set[int]] = {}
    all_card_ids: set[int] = set()
    primary_card_ids: dict[int, int | None] = {}
    for r in req.requirements:
        r_card_ids: set[int] = set()
        if r.material_card_id:
            r_card_ids.add(r.material_card_id)
            primary_card_ids[r.id] = r.material_card_id
        else:
            primary_card_ids[r.id] = None
        for sub in r.substitutes or []:
            sub_str = (sub if isinstance(sub, str) else "").strip()
            if sub_str:
                sub_key = normalize_mpn_key(sub_str)
                if sub_key:
                    sub_card = db.query(MaterialCard.id).filter_by(normalized_mpn=sub_key).first()
                    if sub_card:  # pragma: no cover
                        r_card_ids.add(sub_card[0])
        req_card_map[r.id] = r_card_ids
        all_card_ids |= r_card_ids

    hist_by_req: dict[int, list] = {}
    if all_card_ids:
        hist_query = (
            db.query(Offer)
            .filter(
                Offer.requisition_id != req_id,
                Offer.material_card_id.in_(all_card_ids),
                Offer.status.in_(["active", "won"]),
            )
            .options(joinedload(Offer.entered_by))
            .order_by(Offer.created_at.desc())
            .limit(100)
            .all()
        )
        for ho in hist_query:  # pragma: no cover
            for r in req.requirements:
                if ho.material_card_id in req_card_map.get(r.id, set()):
                    if r.id not in hist_by_req:
                        hist_by_req[r.id] = []
                    is_sub = ho.material_card_id != primary_card_ids.get(r.id)
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
                    break

    result = []
    for r in req.requirements:
        target = float(r.target_price) if r.target_price else None
        last_q = (quoted_prices.get(f"card:{r.material_card_id}") if r.material_card_id else None) or quoted_prices.get(
            (r.primary_mpn or "").upper().strip()
        )
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


async def _fire_deal_condition_alerts(db, offer, req, send_alert_fn):
    """Check and fire Teams DM alerts for deal condition changes.

    Trigger A: New offer has better price than existing best.
    Trigger B: Total sourced qty now meets/exceeds target.
    Both route to req.created_by (the AM/salesperson).
    """
    from sqlalchemy import func

    alerts = []

    # Trigger A: Better price (skip first offer — nothing to compare)
    if offer.unit_price and offer.unit_price > 0:
        best_price = (
            db.query(func.min(Offer.unit_price))
            .filter(Offer.requirement_id == offer.requirement_id, Offer.id != offer.id, Offer.unit_price > 0)
            .scalar()
        )
        if best_price and float(offer.unit_price) < float(best_price):
            alerts.append(
                f"Better price: {offer.vendor_name} on {offer.mpn} "
                f"${offer.unit_price} (was ${best_price})"
                f"{' — ' + req.customer_name if req.customer_name else ''}"
            )

    # Trigger B: Qty filled threshold transition
    requirement = db.get(Requirement, offer.requirement_id) if offer.requirement_id else None
    if requirement and requirement.target_qty and requirement.target_qty > 0:
        total_sourced = (
            db.query(func.sum(Offer.qty_available))
            .filter(Offer.requirement_id == offer.requirement_id, Offer.status == "active")
            .scalar()
        ) or 0
        # Check if we just crossed the threshold (was below before this offer)
        prev_total = total_sourced - (offer.qty_available or 0)
        if total_sourced >= requirement.target_qty and prev_total < requirement.target_qty:
            vendor_count = (
                db.query(func.count(func.distinct(Offer.vendor_name)))
                .filter(Offer.requirement_id == offer.requirement_id, Offer.status == "active")
                .scalar()
            )
            alerts.append(
                f"Qty filled: {offer.mpn} — {total_sourced}/{requirement.target_qty} "
                f"across {vendor_count} vendor{'s' if vendor_count != 1 else ''}"
                f"{' — ' + req.customer_name if req.customer_name else ''}"
            )

    if alerts:
        msg = "\n".join(alerts)
        await send_alert_fn(db, req.created_by, msg, "deal_condition", str(offer.id))


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

    # Sanitize user-entered text fields to prevent stored XSS
    for field in ("vendor_name", "mpn", "manufacturer", "notes", "lead_time",
                  "condition", "date_code", "packaging", "firmware", "hardware_code",
                  "warranty", "country_of_origin"):
        val = getattr(payload, field, None)
        if val and isinstance(val, str):
            setattr(payload, field, sanitize_text(val))

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
            candidates = db.query(VendorCard).filter(VendorCard.normalized_name.ilike(f"{prefix}%")).limit(20).all()
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
    _enrich_new_card = None
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
            get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY")
            or get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY")
        ):
            _enrich_new_card = (card.id, domain, card.display_name)
    # Resolve material card for this MPN
    from ...search_service import resolve_material_card
    from ...utils.normalization import normalize_mpn_key

    mat_card = resolve_material_card(payload.mpn, db)

    offer = Offer(
        requisition_id=req_id,
        requirement_id=payload.requirement_id,
        material_card_id=mat_card.id if mat_card else None,
        normalized_mpn=normalize_mpn_key(payload.mpn) if payload.mpn else None,
        vendor_card_id=card.id,
        vendor_name=card.display_name,
        vendor_name_normalized=card.normalized_name,
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

    # Phase 1: Auto-advance per-part sourcing status when offer is created
    if offer.requirement_id and offer.status == "active":
        try:
            from app.services.requirement_status import on_offer_created

            requirement = db.get(Requirement, offer.requirement_id)
            if requirement:
                on_offer_created(requirement, db, actor=user)
        except Exception as e:
            logger.debug("Requirement status update failed: {}", e)

    db.commit()

    # Phase 1: Notify sales creator that a new offer was entered on their req
    if req.created_by and req.created_by != user.id:
        try:
            existing_notif = (
                db.query(ActivityLog)
                .filter(
                    ActivityLog.user_id == req.created_by,
                    ActivityLog.activity_type == "new_offer",
                    ActivityLog.requisition_id == req_id,
                    ActivityLog.dismissed_at.is_(None),
                )
                .first()
            )
            offer_count = (
                db.query(Offer)
                .filter(Offer.requisition_id == req_id, Offer.status == "active")
                .count()
            )
            new_subj = f"New offer: {offer.vendor_name} — {offer.mpn} (${offer.unit_price or 'TBD'}) · {offer_count} total offers"
            if existing_notif:
                existing_notif.subject = new_subj
                existing_notif.created_at = datetime.now(timezone.utc)
            else:
                db.add(
                    ActivityLog(
                        user_id=req.created_by,
                        activity_type="new_offer",
                        channel="system",
                        requisition_id=req_id,
                        contact_name=offer.vendor_name,
                        subject=new_subj,
                    )
                )
            db.commit()
        except Exception:
            logger.debug("New offer notification failed", exc_info=True)

    # Auto-capture offer facts into Knowledge Ledger
    try:
        from app.services.knowledge_service import capture_offer_fact

        capture_offer_fact(db, offer=offer, user_id=user.id)
    except Exception as e:
        logger.warning("Knowledge auto-capture (offer) failed: {}", e)

    # Reset strategic vendor 39-day clock if this vendor is claimed
    if offer.vendor_card_id:
        try:
            from app.services.strategic_vendor_service import record_offer
            record_offer(db, offer.vendor_card_id)
        except Exception as e:
            logger.debug("Strategic vendor clock reset failed: {}", e)

    # Background vendor enrichment — fire after commit so card is persisted
    if _enrich_new_card:
        from ...utils.vendor_helpers import _background_enrich_vendor

        asyncio.create_task(_background_enrich_vendor(*_enrich_new_card))

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

                asyncio.create_task(
                    send_competitive_quote_alert(
                        offer_id=offer.id,
                        mpn=offer.mpn,
                        vendor_name=offer.vendor_name,
                        offer_price=float(offer.unit_price),
                        best_price=float(best_price),
                        requisition_id=req_id,
                    )
                )
                # Deduplicated in-app notification for requisition owner
                if req.created_by:
                    existing_notif = (
                        db.query(ActivityLog)
                        .filter(
                            ActivityLog.user_id == req.created_by,
                            ActivityLog.activity_type == "competitive_quote",
                            ActivityLog.requisition_id == req_id,
                            ActivityLog.dismissed_at.is_(None),
                        )
                        .first()
                    )
                    new_subj = f"Competitive quote: {offer.vendor_name} — {offer.mpn} at ${offer.unit_price} ({pct}% below best)"
                    if existing_notif:
                        existing_notif.subject = new_subj
                        existing_notif.created_at = datetime.now(timezone.utc)
                    else:
                        db.add(
                            ActivityLog(
                                user_id=req.created_by,
                                activity_type="competitive_quote",
                                channel="system",
                                requisition_id=req_id,
                                contact_name=offer.vendor_name,
                                subject=new_subj,
                            )
                        )
                    db.commit()
    except Exception:
        logger.debug("Activity event creation failed", exc_info=True)

    # Teams DM alerts: better price + qty filled (to requisition owner)
    try:
        if req.created_by and offer.requirement_id:
            from ...services.teams_alert_service import send_alert

            await _fire_deal_condition_alerts(db, offer, req, send_alert)
    except Exception:
        logger.debug("Deal condition alert failed", exc_info=True)

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
    # Validate status transition if status is being changed
    if "status" in changes and changes["status"] != offer.status:
        from ...services.status_machine import validate_transition

        try:
            validate_transition("offer", offer.status, changes["status"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    # Snapshot old values for changelog
    trackable = [
        "vendor_name",
        "qty_available",
        "unit_price",
        "lead_time",
        "condition",
        "warranty",
        "manufacturer",
        "date_code",
        "packaging",
        "moq",
        "notes",
        "status",
    ]
    old_dict = {f: getattr(offer, f) for f in trackable}
    for field, value in changes.items():
        setattr(offer, field, value)
    new_dict = {f: getattr(offer, f) for f in trackable}
    record_changes(db, "offer", offer_id, user.id, old_dict, new_dict, trackable)
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id

    # CPH hook: record purchase history when offer status changes to 'won'
    if old_dict.get("status") != "won" and offer.status == "won":
        _record_offer_won_history(db, offer)

    db.commit()
    return {"ok": True}


@router.delete("/api/offers/{offer_id}")
async def delete_offer(offer_id: int, user: User = Depends(require_buyer), db: Session = Depends(get_db)):
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
    record_changes(db, "offer", offer_id, user.id, {"status": old_status}, {"status": "active"}, ["status"])
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
    record_changes(db, "offer", offer_id, user.id, {"status": old_status}, {"status": "rejected"}, ["status"])
    db.commit()
    return {"ok": True, "status": "rejected"}


@router.patch("/api/offers/{offer_id}/mark-sold")
async def mark_offer_sold(
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark an offer as sold — stock is confirmed purchased/gone.

    Only the buyer who created the offer or an admin can mark it sold.
    """
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.entered_by_id != user.id and not _is_admin(user):
        raise HTTPException(403, "Only the offer creator or an admin can mark sold")
    if offer.status == "sold":
        return {"ok": True, "status": "sold", "message": "Already marked sold"}
    old_status = offer.status
    offer.status = "sold"
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    record_changes(db, "offer", offer_id, user.id, {"status": old_status}, {"status": "sold"}, ["status"])
    db.commit()
    return {"ok": True, "status": "sold"}


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
    drive_path = f"/me/drive/root:/AvailAI/Offers/{offer.requisition_id}/{safe_name}:/content"
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


def _record_offer_won_history(db: Session, offer: Offer) -> None:
    """Feed customer_part_history when an offer is marked as won.

    Resolves the company via offer → requisition → customer_site → company.
    Errors are logged but never block the offer update flow.
    """
    if not offer.material_card_id:
        return
    try:
        from ...models import CustomerSite
        from ...services.purchase_history_service import upsert_purchase

        req = db.get(Requisition, offer.requisition_id) if offer.requisition_id else None
        if not req or not req.customer_site_id:
            return
        site = db.get(CustomerSite, req.customer_site_id)
        if not site or not site.company_id:  # pragma: no cover
            return

        upsert_purchase(
            db,
            company_id=site.company_id,
            material_card_id=offer.material_card_id,
            source="avail_offer",
            unit_price=offer.unit_price,
            quantity=offer.qty_available,
            source_ref=f"offer:{offer.id}",
        )
    except Exception as e:
        logger.warning("Offer won purchase history recording failed: %s", e)


# ── Review Queue — medium-confidence AI-parsed offers ─────────────────


@router.get("/api/offers/review-queue")
async def list_review_queue(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List offers needing human review (evidence_tier T4, status pending_review).

    Called by: frontend review queue panel
    Depends on: Offer model with evidence_tier column
    """
    offers = (
        db.query(Offer)
        .filter(
            Offer.evidence_tier == "T4",
            Offer.status == "pending_review",
        )
        .order_by(Offer.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": o.id,
            "requisition_id": o.requisition_id,
            "vendor_name": o.vendor_name,
            "mpn": o.mpn,
            "qty_available": o.qty_available,
            "unit_price": float(o.unit_price) if o.unit_price else None,
            "parse_confidence": o.parse_confidence,
            "evidence_tier": o.evidence_tier,
            "source": o.source,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o in offers
    ]


@router.post("/api/offers/{offer_id}/promote")
async def promote_offer(
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Promote a T4 (medium-confidence) offer to T5 after human review.

    Called by: review queue UI
    Depends on: Offer model with promoted_by_id, promoted_at, evidence_tier
    """
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    if offer.evidence_tier != "T4":
        raise HTTPException(400, "Only T4 offers can be promoted")

    offer.evidence_tier = "T5"
    offer.status = "active"
    offer.promoted_by_id = user.id
    offer.promoted_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(f"Offer {offer_id} promoted T4→T5 by user {user.id}")
    return {"status": "promoted", "offer_id": offer_id}


# Note: POST /api/offers/{offer_id}/reject was a duplicate of PUT /api/offers/{offer_id}/reject
# (line 689). The PUT version with audit trail (record_changes) is the canonical endpoint.
# The duplicate POST was removed to prevent undefined routing behavior.

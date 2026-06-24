from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session, joinedload, selectinload

from ...constants import (
    AccessKey,
    ActivityType,
    OfferStatus,
    RequisitionStatus,
    UserRole,
)
from ...database import get_db
from ...dependencies import is_admin as _is_admin
from ...dependencies import require_access, require_buyer, require_requisition_access, require_user
from ...models import (
    ActivityLog,
    ChangeLog,
    MaterialCard,
    Offer,
    OfferAttachment,
    Quote,
    Requirement,
    User,
    VendorCard,
    VendorReview,
)
from ...schemas.crm import OfferCreate, OfferUpdate, OneDriveAttach
from ...schemas.responses import OfferListResponse
from ...services import attachment_service
from ...services.activity_service import log_activity
from ...services.credential_service import get_credential_cached
from ...services.status_machine import require_valid_transition
from ...services.vendor_unavailability import maybe_release_on_offer
from ...utils.async_helpers import safe_background_task
from ...utils.normalization import normalize_mpn_key
from ...vendor_utils import normalize_vendor_name
from ._helpers import _preload_last_quoted_prices, record_changes

router = APIRouter()


def _upsert_inapp_notice(
    db: Session,
    *,
    user_id: int,
    activity_type: str,
    requisition_id: int,
    contact_name: str,
    subject: str,
) -> None:
    """Create or refresh a deduplicated in-app (system-channel) notification.

    Looks for an existing non-dismissed ActivityLog of the same type on the same
    requisition for the same user; if found, refreshes its subject + created_at,
    otherwise inserts a new system-channel row. Does not commit — the caller owns the
    transaction boundary.
    """
    existing_notif = (
        db.query(ActivityLog)
        .filter(
            ActivityLog.user_id == user_id,
            ActivityLog.activity_type == activity_type,
            ActivityLog.requisition_id == requisition_id,
            ActivityLog.dismissed_at.is_(None),
        )
        .first()
    )
    if existing_notif:
        existing_notif.subject = subject
        existing_notif.created_at = datetime.now(timezone.utc)
    else:
        db.add(
            ActivityLog(
                user_id=user_id,
                activity_type=activity_type,
                channel="system",
                requisition_id=requisition_id,
                contact_name=contact_name,
                subject=subject,
            )
        )


def _log_offer_status_change(db: Session, offer: Offer, old_status, user: User) -> None:
    """Emit the standard OFFER_STATUS_CHANGED activity log for an offer transition."""
    log_activity(
        db,
        activity_type=ActivityType.OFFER_STATUS_CHANGED,
        requisition_id=offer.requisition_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer {offer.vendor_name} status: {old_status} → {offer.status}",
        details={
            "offer_id": offer.id,
            "old_status": str(old_status),
            "new_status": str(offer.status),
        },
    )


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
    if user.role == UserRole.BUYER:
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

    groups: dict[int, list] = {}
    for o in offers:
        key = o.requirement_id or 0
        atts = [attachment_service.serialize(a) for a in (o.attachments or [])]
        groups.setdefault(key, []).append(
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
                    is_sub = ho.material_card_id != primary_card_ids.get(r.id)
                    hist_by_req.setdefault(r.id, []).append(
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
        valid_until=payload.valid_until,
        source=payload.source,
        vendor_response_id=payload.vendor_response_id,
        entered_by_id=user.id,
        notes=payload.notes,
        status=payload.status,
    )
    from app.services.offer_qualification import apply_qualification

    offer.qualification = payload.qualification or None
    # Non-raising: composes the standardized note + sets qualification_status. The
    # essentials gate is enforced at the buyer handlers, not in this canonical builder.
    apply_qualification(offer)
    db.add(offer)
    old_status = req.status
    if req.status in (RequisitionStatus.ACTIVE, RequisitionStatus.SOURCING):
        from ...services.requisition_state import transition as req_transition

        try:
            req_transition(req, "offers", user, db)
        except ValueError:
            pass  # already in offers or later state

    # Phase 1: Auto-advance per-part sourcing status when offer is created
    if offer.requirement_id and offer.status == OfferStatus.ACTIVE:
        try:
            from app.services.requirement_status import on_offer_created

            requirement = db.get(Requirement, offer.requirement_id)
            if requirement:
                on_offer_created(requirement, db, actor=user)
        except Exception as e:
            logger.warning("Requirement status update failed: {}", e)

    db.flush()  # offer.id populated; activity row + offer committed together below

    # Offer hook: a user-entered ACTIVE offer is proof of availability — release the
    # vendor's matching active unavailability records ('offer_received'). Same
    # session/commit as the offer itself.
    if offer.status == OfferStatus.ACTIVE:
        maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user)

    log_activity(
        db,
        activity_type=ActivityType.OFFER_CREATED,
        requisition_id=offer.requisition_id,
        requirement_id=offer.requirement_id,
        user_id=user.id,
        vendor_card_id=offer.vendor_card_id,
        description=f"Offer added: {offer.vendor_name} — {offer.mpn}",
        details={"offer_id": offer.id, "source": offer.source},
    )
    db.commit()

    # Auto-generate review task for new offer
    try:
        from app.services.task_service import on_offer_received

        on_offer_received(db, offer.requisition_id, offer.vendor_name, offer.mpn, offer.id)
    except Exception:
        logger.warning("Task auto-gen for offer failed", exc_info=True)

    # Phase 1: Notify sales creator that a new offer was entered on their req
    if req.created_by and req.created_by != user.id:
        try:
            offer_count = (
                db.query(Offer).filter(Offer.requisition_id == req_id, Offer.status == OfferStatus.ACTIVE).count()
            )
            new_subj = f"New offer: {offer.vendor_name} — {offer.mpn} (${offer.unit_price or 'TBD'}) · {offer_count} total offers"
            _upsert_inapp_notice(
                db,
                user_id=req.created_by,
                activity_type="new_offer",
                requisition_id=req_id,
                contact_name=offer.vendor_name,
                subject=new_subj,
            )
            db.commit()
        except Exception:
            logger.warning("New offer notification failed", exc_info=True)

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
            logger.warning("Strategic vendor clock reset failed: {}", e)

    # Background vendor enrichment — fire after commit so card is persisted
    if _enrich_new_card:
        from ...utils.vendor_helpers import _background_enrich_vendor

        await safe_background_task(_background_enrich_vendor(*_enrich_new_card), task_name="enrich_vendor_from_offer")

    # Competitive quote alert: in-app notification if >20% below current best price
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
                # Deduplicated in-app notification for requisition owner
                if req.created_by:
                    new_subj = f"Competitive quote: {offer.vendor_name} — {offer.mpn} at ${offer.unit_price} ({pct}% below best)"
                    _upsert_inapp_notice(
                        db,
                        user_id=req.created_by,
                        activity_type="competitive_quote",
                        requisition_id=req_id,
                        contact_name=offer.vendor_name,
                        subject=new_subj,
                    )
                    db.commit()
    except Exception:
        logger.warning("Activity event creation failed", exc_info=True)

    # Notify requisition creator via SSE that a new offer/quote was added
    notify_user_id = req.created_by if req.created_by and req.created_by != user.id else user.id
    try:
        from ...services.sse_broker import broker

        await broker.publish(
            f"user:{notify_user_id}",
            "quote_updated",
            '{"offer_id": ' + str(offer.id) + ', "requisition_id": ' + str(req_id) + "}",
        )
    except Exception:
        logger.warning("SSE quote_updated notification failed", exc_info=True)

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
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    changes = payload.model_dump(exclude_unset=True)
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
    if "status" in changes and changes["status"] != offer.status:
        require_valid_transition("offer", offer.status, changes["status"])
    for field, value in changes.items():
        setattr(offer, field, value)
    new_dict = {f: getattr(offer, f) for f in trackable}
    record_changes(db, "offer", offer_id, user.id, old_dict, new_dict, trackable)
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id

    from app.services.offer_qualification import apply_qualification

    if "qualification" in changes:
        offer.qualification = changes["qualification"] or None
    # Non-raising: composes the standardized note + sets qualification_status. The
    # essentials gate is enforced at the buyer handlers, not in this canonical builder.
    apply_qualification(offer)

    db.commit()
    return {"ok": True}


@router.delete("/api/offers/{offer_id}")
async def delete_offer(offer_id: int, user: User = Depends(require_buyer), db: Session = Depends(get_db)):
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    db.delete(offer)
    db.commit()
    return {"ok": True}


@router.put("/api/offers/{offer_id}/reconfirm")
async def reconfirm_offer(
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Mark a historical offer as reconfirmed (still valid)."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
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
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Approve a pending_review offer → active."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    if offer.status != "pending_review":
        raise HTTPException(400, "Only pending_review offers can be approved")
    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.ACTIVE)
    offer.status = OfferStatus.ACTIVE
    offer.approved_by_id = user.id
    offer.approved_at = datetime.now(timezone.utc)
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    record_changes(db, "offer", offer_id, user.id, {"status": old_status}, {"status": "active"}, ["status"])
    # Offer hook: user approval of a pending offer is user-initiated proof of
    # availability — release the vendor's matching active unavailability records.
    maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user)
    _log_offer_status_change(db, offer, old_status, user)
    db.commit()
    return {"ok": True, "status": "active"}


@router.put("/api/offers/{offer_id}/reject")
async def reject_offer(
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
    reason: str = "",
):
    """Reject a pending_review offer."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    if offer.status != "pending_review":
        raise HTTPException(400, "Only pending_review offers can be rejected")
    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.REJECTED)
    offer.status = OfferStatus.REJECTED
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    if reason:
        offer.notes = f"{offer.notes or ''}\n[Rejected: {reason}]".strip()
    record_changes(db, "offer", offer_id, user.id, {"status": old_status}, {"status": "rejected"}, ["status"])
    _log_offer_status_change(db, offer, old_status, user)
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
    if offer.status == OfferStatus.SOLD:
        return {"ok": True, "status": "sold", "message": "Already marked sold"}
    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.SOLD)
    offer.status = OfferStatus.SOLD
    offer.updated_at = datetime.now(timezone.utc)
    offer.updated_by_id = user.id
    record_changes(db, "offer", offer_id, user.id, {"status": old_status}, {"status": "sold"}, ["status"])
    _log_offer_status_change(db, offer, old_status, user)
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


@router.get("/api/offers/{offer_id}/attachments")
async def list_offer_attachments(
    offer_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List attachments on an offer (HTML for HTMX, JSON otherwise).

    Access matches the rest of the offer endpoints: gated on offer existence.
    """
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    return attachment_service.attachment_list_response(
        request, kind="offer", entity_id=offer_id, rows=offer.attachments
    )


@router.post("/api/offers/{offer_id}/attachments")
async def upload_offer_attachment(
    offer_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file to OneDrive/SharePoint and attach it to an offer."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    att = await attachment_service.store_and_attach(
        db,
        model=OfferAttachment,
        fk_field="offer_id",
        entity_label="Offers",
        entity_id=offer_id,
        file=file,
        user=user,
    )
    return attachment_service.serialize(att)


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
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
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
        library_item_id=item_id,
        library_web_url=item.get("webUrl"),
        content_type=item.get("file", {}).get("mimeType"),
        size_bytes=item.get("size"),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return attachment_service.serialize(att)


@router.delete("/api/offer-attachments/{att_id}")
async def delete_offer_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete an offer attachment (and remove from cloud storage)."""
    att = db.get(OfferAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    parent_offer = db.get(Offer, att.offer_id)
    require_requisition_access(
        db,
        parent_offer.requisition_id if parent_offer else None,
        user,
        owner_id=parent_offer.entered_by_id if parent_offer else None,
        label="Attachment",
    )
    return await attachment_service.remove_attachment(db, att, user)


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
    endpoint = f"/me/drive/root:/{path}:/children" if path else "/me/drive/root/children"
    data = await gc.get_json(
        endpoint,
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
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Promote a T4 (medium-confidence) offer to T5 after human review.

    Called by: review queue UI
    Depends on: Offer model with promoted_by_id, promoted_at, evidence_tier
    """
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    if offer.evidence_tier != "T4":
        raise HTTPException(400, "Only T4 offers can be promoted")

    offer.evidence_tier = "T5"
    require_valid_transition("offer", offer.status, OfferStatus.ACTIVE)
    old_status = offer.status
    offer.status = OfferStatus.ACTIVE
    offer.promoted_by_id = user.id
    offer.promoted_at = datetime.now(timezone.utc)
    # Offer hook: user promotion of a pending offer is user-initiated proof of
    # availability — release the vendor's matching active unavailability records.
    maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user)
    _log_offer_status_change(db, offer, old_status, user)
    db.commit()

    logger.info(f"Offer {offer_id} promoted T4→T5 by user {user.id}")
    return {"status": "promoted", "offer_id": offer_id}


@router.post("/api/offers/{offer_id}/reject")
async def reject_offer_t4_review(
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Reject a T4 offer — marks as rejected, keeps for audit trail.

    Called by: review queue UI
    Depends on: Offer model
    """
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    if offer.status != "pending_review":
        raise HTTPException(400, "Only pending_review offers can be rejected")

    require_valid_transition("offer", offer.status, OfferStatus.REJECTED)
    old_status = offer.status
    offer.status = OfferStatus.REJECTED
    offer.updated_by_id = user.id
    offer.updated_at = datetime.now(timezone.utc)
    _log_offer_status_change(db, offer, old_status, user)
    db.commit()

    logger.info(f"Offer {offer_id} rejected by user {user.id}")
    return {"status": "rejected", "offer_id": offer_id}

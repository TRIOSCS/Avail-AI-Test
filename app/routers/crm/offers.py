from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import (
    AccessKey,
    ActivityType,
    OfferStatus,
    RequisitionStatus,
)
from ...database import get_db
from ...dependencies import (
    require_access,
    require_buyer,
    require_requisition_access,
    require_user,
)
from ...models import (
    Offer,
    OfferAttachment,
    Requirement,
    User,
    VendorCard,
)
from ...schemas.crm import OfferCreate, OfferUpdate
from ...services import attachment_service
from ...services.activity_service import log_activity
from ...services.credential_service import get_credential_cached
from ...services.status_machine import require_valid_transition
from ...services.vendor_unavailability import maybe_release_on_offer
from ...utils.async_helpers import safe_background_task
from ...utils.normalization import normalize_mpn_key
from ...utils.sql_helpers import escape_like
from ...vendor_utils import normalize_vendor_name
from ._helpers import record_changes

router = APIRouter()


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
    offer.updated_at = datetime.now(UTC)
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
    if offer.status != OfferStatus.PENDING_REVIEW:
        raise HTTPException(400, "Only pending_review offers can be approved")
    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.ACTIVE)
    offer.status = OfferStatus.ACTIVE
    offer.approved_by_id = user.id
    offer.approved_at = datetime.now(UTC)
    offer.updated_at = datetime.now(UTC)
    offer.updated_by_id = user.id
    record_changes(db, "offer", offer_id, user.id, {"status": old_status}, {"status": "active"}, ["status"])
    # Offer hook: user approval of a pending offer is user-initiated proof of
    # availability — release the vendor's matching active unavailability records.
    maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user, offer_condition=offer.condition)
    _log_offer_status_change(db, offer, old_status, user)
    db.commit()

    from ...services.proactive_matching import trigger_rematch_on_offer_approval

    trigger_rematch_on_offer_approval(db, offer)

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
    if offer.status != OfferStatus.PENDING_REVIEW:
        raise HTTPException(400, "Only pending_review offers can be rejected")
    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.REJECTED)
    offer.status = OfferStatus.REJECTED
    offer.updated_at = datetime.now(UTC)
    offer.updated_by_id = user.id
    if reason:
        offer.notes = f"{offer.notes or ''}\n[Rejected: {reason}]".strip()
    record_changes(db, "offer", offer_id, user.id, {"status": old_status}, {"status": "rejected"}, ["status"])
    _log_offer_status_change(db, offer, old_status, user)
    db.commit()
    return {"ok": True, "status": "rejected"}


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
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
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


# ── Review Queue — medium-confidence AI-parsed offers ─────────────────


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
    if offer.status != OfferStatus.PENDING_REVIEW:
        raise HTTPException(400, "Only pending_review offers can be rejected")

    require_valid_transition("offer", offer.status, OfferStatus.REJECTED)
    old_status = offer.status
    offer.status = OfferStatus.REJECTED
    offer.updated_by_id = user.id
    offer.updated_at = datetime.now(UTC)
    _log_offer_status_change(db, offer, old_status, user)
    db.commit()

    logger.info(f"Offer {offer_id} rejected by user {user.id}")
    return {"status": "rejected", "offer_id": offer_id}


# W2 integration restore: these three are consumed by app/routers/sightings.py
# via request-time lazy imports (invisible to route-reference grep) — the manifest
# orphan verdict was wrong for them. W3 offer_service consolidation retires them.
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
                .filter(VendorCard.normalized_name.ilike(f"{escape_like(prefix)}%", escape="\\"))
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
    if req.status == RequisitionStatus.OPEN:
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
        maybe_release_on_offer(db, offer.requirement_id, offer.vendor_name, user, offer_condition=offer.condition)

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

    # W2.9: new-offer in-app notification removed with the write-only
    # Notification table — email via the approval outbox is the one system.

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

    # W2.9: competitive-quote in-app alert removed with the write-only
    # Notification table (email path via approval outbox is the one system).

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
    offer.reconfirmed_at = datetime.now(UTC)
    offer.reconfirm_count = (offer.reconfirm_count or 0) + 1
    db.commit()
    return {
        "ok": True,
        "reconfirmed_at": offer.reconfirmed_at.isoformat(),
        "reconfirm_count": offer.reconfirm_count,
    }


@router.patch("/api/offers/{offer_id}/mark-sold")
async def mark_offer_sold(
    offer_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Mark an offer as sold — stock is confirmed purchased/gone.

    Gated to the requisition owner (or buyer/manager/admin) via
    require_requisition_access, matching the sibling offer-mutation routes; restricted
    (SALES/TRADER) non-owners 404.
    """
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    if offer.status == OfferStatus.SOLD:
        return {"ok": True, "status": "sold", "message": "Already marked sold"}
    old_status = offer.status
    require_valid_transition("offer", offer.status, OfferStatus.SOLD)
    offer.status = OfferStatus.SOLD
    offer.updated_at = datetime.now(UTC)
    offer.updated_by_id = user.id
    record_changes(db, "offer", offer_id, user.id, {"status": old_status}, {"status": "sold"}, ["status"])
    _log_offer_status_change(db, offer, old_status, user)
    db.commit()
    return {"ok": True, "status": "sold"}

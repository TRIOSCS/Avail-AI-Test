"""routers/crm/offers.py — surviving JSON offer routes (thin, W3).

Offer update/delete, pending-review approve/reject (offer_card.html buttons), the
T4 review-queue reject, and OneDrive attachments. All business logic lives in
app/services/offer_service.py (W3 "one offer_service", spec §9) — these handlers
only do lookup + authz + response shaping.

The legacy JSON create (POST /api/requisitions/{req_id}/offers), non-TTL
reconfirm, and mark-sold handlers were retired in W3: their only remaining
callers (routers/sightings.py wrappers) now call offer_service directly, and the
canonical TTL-resetting reconfirm lives in the service.

Called by: app/routers/crm/__init__.py (router mount), templates
    shared/offer_card.html + shared/_attachments.html.
Depends on: app.services.offer_service, app.services.attachment_service,
    app.dependencies, app.models.
"""

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy.orm import Session

from ...constants import AccessKey
from ...database import get_db
from ...dependencies import (
    require_access,
    require_buyer,
    require_requisition_access,
    require_user,
)
from ...models import Offer, OfferAttachment, User
from ...schemas.crm import OfferUpdate
from ...services import attachment_service, offer_service

router = APIRouter()


def _offer_or_404(db: Session, offer_id: int, user: User) -> Offer:
    """Load an offer and enforce requisition-scoped access (owner fallback)."""
    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    require_requisition_access(db, offer.requisition_id, user, owner_id=offer.entered_by_id, label="Offer")
    return offer


# ── Offers ───────────────────────────────────────────────────────────────


@router.put("/api/offers/{offer_id}")
async def update_offer(
    offer_id: int,
    payload: OfferUpdate,
    user: User = Depends(require_buyer),
    db: Session = Depends(get_db),
):
    offer = _offer_or_404(db, offer_id, user)
    offer_service.update_offer(db, offer, payload, user)
    return {"ok": True}


@router.delete("/api/offers/{offer_id}")
async def delete_offer(offer_id: int, user: User = Depends(require_buyer), db: Session = Depends(get_db)):
    offer = _offer_or_404(db, offer_id, user)
    offer_service.delete_offer(db, offer, user)
    return {"ok": True}


@router.put("/api/offers/{offer_id}/approve")
async def approve_offer(
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
):
    """Approve a pending_review offer → active."""
    offer = _offer_or_404(db, offer_id, user)
    offer_service.approve_offer(db, offer, user)
    return {"ok": True, "status": "active"}


@router.put("/api/offers/{offer_id}/reject")
async def reject_offer(
    offer_id: int,
    user: User = Depends(require_access(AccessKey.APPROVE_OFFERS)),
    db: Session = Depends(get_db),
    reason: str = "",
):
    """Reject a pending_review offer."""
    offer = _offer_or_404(db, offer_id, user)
    offer_service.reject_offer(db, offer, user, reason=reason)
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
    offer = _offer_or_404(db, offer_id, user)
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
    _offer_or_404(db, offer_id, user)
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
    offer = _offer_or_404(db, offer_id, user)
    offer_service.reject_offer(db, offer, user)
    logger.info(f"Offer {offer_id} rejected by user {user.id}")
    return {"status": "rejected", "offer_id": offer_id}

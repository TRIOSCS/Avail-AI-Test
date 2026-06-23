"""attachments_extra.py — Unified serve route for all attachment entities.

Provides GET /api/attachments/{kind}/{att_id}/content — resolves the row from
the correct attachment table by kind, enforces per-kind ownership, then delegates
to attachment_service.open_attachment.

Called by: app/main.py (router registration)
Depends on: app/services/attachment_service, all attachment models, app/dependencies
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import get_req_for_user, require_user
from ..models import (
    CompanyAttachment,
    MaterialCardAttachment,
    Offer,
    OfferAttachment,
    RequirementAttachment,
    RequisitionAttachment,
    SiteContactAttachment,
    User,
)
from ..services import attachment_service

router = APIRouter()

_KIND_MODEL = {
    "requisition": RequisitionAttachment,
    "requirement": RequirementAttachment,
    "offer": OfferAttachment,
    "company": CompanyAttachment,
    "contact": SiteContactAttachment,
    "material": MaterialCardAttachment,
}


def _check_serve_access(kind: str, att, user: User, db: Session) -> None:
    """Enforce per-kind ownership before serving an attachment.

    Raises HTTPException(404) on access denial to avoid existence leaks. Matches the
    same gate logic that each kind's list endpoint uses.
    """
    if kind == "requisition":
        if not get_req_for_user(db, user, att.requisition_id):
            raise HTTPException(404, "Attachment not found")
    elif kind == "requirement":
        # att.requirement is a lazy-loaded relationship; resolve to its requisition_id.
        req_id = att.requirement.requisition_id if att.requirement else None
        if req_id is None or not get_req_for_user(db, user, req_id):
            raise HTTPException(404, "Attachment not found")
    elif kind == "offer":
        # Offer endpoints gate only on offer existence — match that level here.
        if not db.get(Offer, att.offer_id):
            raise HTTPException(404, "Attachment not found")
    else:
        # TODO(Task 4): enforce company/contact/material ownership here once their helpers land
        pass


@router.get("/api/attachments/{kind}/{att_id}/content")
async def serve_attachment(
    kind: str,
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Stream or redirect an attachment by kind and ID."""
    model = _KIND_MODEL.get(kind)
    if model is None:
        raise HTTPException(404, f"Unknown attachment kind: {kind!r}")
    att = db.get(model, att_id)
    if att is None:
        raise HTTPException(404, "Attachment not found")
    _check_serve_access(kind, att, user, db)
    return await attachment_service.open_attachment(att, user)

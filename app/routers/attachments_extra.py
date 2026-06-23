"""attachments_extra.py — Unified serve route for all attachment entities.

Provides GET /api/attachments/{kind}/{att_id}/content — resolves the row from
the correct attachment table by kind, then delegates to attachment_service.open_attachment.

Called by: app/main.py (router registration)
Depends on: app/services/attachment_service, all attachment models, app/dependencies
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import (
    CompanyAttachment,
    MaterialCardAttachment,
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
    return await attachment_service.open_attachment(att, user)

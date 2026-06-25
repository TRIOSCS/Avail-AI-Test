"""File attachment endpoints for requisitions and requirements (OneDrive/SharePoint).

Business Rules:
- Attachments are stored via attachment_service (OneDrive or SharePoint library)
- Max file size: 10 MB (enforced by service)
- Files stored under /AvailAI/Requisitions/{id}/ or /AvailAI/Requirements/{id}/
- Deleting an attachment also removes it from cloud storage (best-effort)
- Existing OneDrive files can be linked without re-uploading

Called by: requisitions.__init__ (sub-router)
Depends on: models, dependencies, services/attachment_service
"""

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import get_req_for_user, require_user
from ...models import (
    Requirement,
    RequirementAttachment,
    RequisitionAttachment,
    User,
)
from ...services import attachment_service
from ...services.attachment_service import attachment_list_response

router = APIRouter(tags=["requisitions"])


@router.get("/api/requisitions/{req_id}/attachments")
async def list_requisition_attachments(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all file attachments on a requisition (HTML for HTMX, JSON otherwise)."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    return attachment_list_response(request, kind="requisition", entity_id=req_id, rows=req.attachments)


@router.post("/api/requisitions/{req_id}/attachments")
async def upload_requisition_attachment(
    req_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file to OneDrive/SharePoint and attach it to a requisition."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    att = await attachment_service.store_and_attach(
        db,
        model=RequisitionAttachment,
        fk_field="requisition_id",
        entity_label="Requisitions",
        entity_id=req_id,
        file=file,
        user=user,
    )
    return attachment_service.serialize(att)


@router.post("/api/requisitions/{req_id}/attachments/onedrive")
async def attach_requisition_from_onedrive(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Attach an existing OneDrive file to a requisition by item ID."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    body = await request.json()
    item_id = body.get("item_id")
    if not item_id:
        raise HTTPException(400, "item_id is required")
    from ...scheduler import get_valid_token

    token = await get_valid_token(user, db)
    if not token:
        raise HTTPException(401, "Microsoft token expired — please re-authenticate")
    from ...utils.graph_client import GraphClient

    gc = GraphClient(token)
    item = await gc.get_json(f"/me/drive/items/{item_id}")
    if "error" in item:
        error_code = item.get("error", {}).get("code", "") if isinstance(item.get("error"), dict) else ""
        if error_code in ("InvalidAuthenticationToken", "TokenExpired"):
            raise HTTPException(401, "Microsoft token expired — please re-authenticate")
        if error_code in ("accessDenied", "AccessDenied"):
            raise HTTPException(403, "Access denied to OneDrive item")
        raise HTTPException(404, "OneDrive item not found")
    att = RequisitionAttachment(
        requisition_id=req_id,
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


@router.delete("/api/requisition-attachments/{att_id}")
async def delete_requisition_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requisition attachment (and remove from cloud storage)."""
    att = db.get(RequisitionAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if not get_req_for_user(db, user, att.requisition_id):
        raise HTTPException(404, "Attachment not found")
    return await attachment_service.remove_attachment(db, att, user)


@router.get("/api/requirements/{req_id}/attachments")
async def list_requirement_attachments(
    req_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all file attachments on a requirement (HTML for HTMX, JSON otherwise)."""
    requirement = db.get(Requirement, req_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    parent_req = get_req_for_user(db, user, requirement.requisition_id)
    if not parent_req:
        raise HTTPException(403, "Not authorized")
    return attachment_list_response(request, kind="requirement", entity_id=req_id, rows=requirement.attachments)


@router.post("/api/requirements/{req_id}/attachments")
async def upload_requirement_attachment(
    req_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file to OneDrive/SharePoint and attach it to a requirement."""
    requirement = db.get(Requirement, req_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    parent_req = get_req_for_user(db, user, requirement.requisition_id)
    if not parent_req:
        raise HTTPException(403, "Not authorized")
    att = await attachment_service.store_and_attach(
        db,
        model=RequirementAttachment,
        fk_field="requirement_id",
        entity_label="Requirements",
        entity_id=req_id,
        file=file,
        user=user,
    )
    return attachment_service.serialize(att)


@router.delete("/api/requirement-attachments/{att_id}")
async def delete_requirement_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requirement attachment (and remove from cloud storage)."""
    att = db.get(RequirementAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    req_id = att.requirement.requisition_id if att.requirement else None
    if req_id is None or not get_req_for_user(db, user, req_id):
        raise HTTPException(404, "Attachment not found")
    return await attachment_service.remove_attachment(db, att, user)

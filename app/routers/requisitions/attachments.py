"""File attachment endpoints for requisitions and requirements (OneDrive).

Business Rules:
- Attachments are uploaded to OneDrive via Microsoft Graph API
- Max file size: 10 MB
- Files stored under /AvailAI/Requisitions/{id}/ or /AvailAI/Requirements/{id}/
- Deleting an attachment also removes it from OneDrive (best-effort)
- Existing OneDrive files can be linked without re-uploading

Called by: requisitions.__init__ (sub-router)
Depends on: models, dependencies, http_client, graph_client
"""

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from loguru import logger
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import get_req_for_user, require_user
from ...models import (
    Requirement,
    RequirementAttachment,
    RequisitionAttachment,
    User,
)

router = APIRouter(tags=["requisitions"])


@router.get("/api/requisitions/{req_id}/attachments")
async def list_requisition_attachments(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all file attachments on a requisition."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    return [
        {
            "id": a.id,
            "file_name": a.file_name,
            "onedrive_url": a.onedrive_url,
            "content_type": a.content_type,
            "size_bytes": a.size_bytes,
            "uploaded_by": a.uploaded_by.name if a.uploaded_by else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in req.attachments
    ]


@router.post("/api/requisitions/{req_id}/attachments")
async def upload_requisition_attachment(
    req_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file to OneDrive and attach it to a requisition."""
    req = get_req_for_user(db, user, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10 MB)")
    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected — please re-login")
    from ...http_client import http

    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    drive_path = f"/me/drive/root:/AvailAI/Requisitions/{req_id}/{safe_name}:/content"
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
    att = RequisitionAttachment(
        requisition_id=req_id,
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
    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected")
    from ...utils.graph_client import GraphClient

    gc = GraphClient(user.access_token)
    item = await gc.get_json(f"/me/drive/items/{item_id}")
    if "error" in item:
        raise HTTPException(404, "OneDrive item not found")
    att = RequisitionAttachment(
        requisition_id=req_id,
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


@router.delete("/api/requisition-attachments/{att_id}")
async def delete_requisition_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requisition attachment (and remove from OneDrive)."""
    att = db.get(RequisitionAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if att.onedrive_item_id and user.access_token:
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


@router.get("/api/requirements/{req_id}/attachments")
async def list_requirement_attachments(
    req_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """List all file attachments on a requirement."""
    requirement = db.get(Requirement, req_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    parent_req = get_req_for_user(db, user, requirement.requisition_id)
    if not parent_req:
        raise HTTPException(403, "Not authorized")
    return [
        {
            "id": a.id,
            "file_name": a.file_name,
            "onedrive_url": a.onedrive_url,
            "content_type": a.content_type,
            "size_bytes": a.size_bytes,
            "uploaded_by": a.uploaded_by.name if a.uploaded_by else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in requirement.attachments
    ]


@router.post("/api/requirements/{req_id}/attachments")
async def upload_requirement_attachment(
    req_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Upload a file to OneDrive and attach it to a requirement."""
    requirement = db.get(Requirement, req_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    parent_req = get_req_for_user(db, user, requirement.requisition_id)
    if not parent_req:
        raise HTTPException(403, "Not authorized")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10 MB)")
    if not user.access_token:
        raise HTTPException(401, "Microsoft account not connected — please re-login")
    from ...http_client import http

    safe_name = file.filename.replace("/", "_").replace("\\", "_")
    drive_path = f"/me/drive/root:/AvailAI/Requirements/{req_id}/{safe_name}:/content"
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
    att = RequirementAttachment(
        requirement_id=req_id,
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


@router.delete("/api/requirement-attachments/{att_id}")
async def delete_requirement_attachment(
    att_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Delete a requirement attachment (and remove from OneDrive)."""
    att = db.get(RequirementAttachment, att_id)
    if not att:
        raise HTTPException(404, "Attachment not found")
    if att.onedrive_item_id and user.access_token:
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

"""attachment_service.py — shared, model-agnostic file-attachment service.

Centralises the upload/serve/delete lifecycle for ALL attachment entities
(Requisition, Requirement, Offer, Company, SiteContact, MaterialCard).

Storage backend is chosen once per upload, based on config, and recorded on
the row so mixed-era rows keep working after IT delivers the company drive ID:
    library_drive_id IS NULL  → user-OneDrive fallback (user token)
    library_drive_id non-NULL → company SharePoint library (app token)

Honest errors only — no silent fallbacks on error (fallback is a *config*
decision, not an error-handling decision).

Called by: app/routers/requisitions/attachments.py, app/routers/crm/offers.py,
           app/routers/attachments_extra.py (Task 3 / Task 4)
Depends on: app/config.py, app/services/datasheet_library.py,
            app/http_client.py, app/scheduler.py, app/constants.py
"""

from __future__ import annotations

import os
from io import BytesIO

from fastapi import HTTPException, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import ALLOWED_ATTACHMENT_EXTENSIONS, MAX_ATTACHMENT_BYTES

_GRAPH = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    """Sanitize a filename for use as a Graph path segment."""
    return (name or "unnamed_file").replace("/", "_").replace("\\", "_")


def _validate(file: UploadFile, content: bytes) -> None:
    """Raise HTTPException(400) if the file exceeds the size limit or has a disallowed
    extension."""
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(400, "File too large (max 10 MB)")
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_ATTACHMENT_EXTENSIONS))
        raise HTTPException(
            400,
            f"File type '{ext}' not allowed. Accepted: {allowed}",
        )


async def _store(
    content: bytes,
    *,
    content_type: str,
    file_name: str,
    entity_label: str,
    entity_id: int,
    user,
    db: Session,
) -> tuple[str | None, str | None, str | None]:
    """Upload content to the correct backend.

    Returns (item_id, drive_id, web_url). drive_id is non-None iff the company-library
    backend was used.
    """
    drive_id = settings.datasheet_library_drive_id

    if drive_id:
        # --- Company SharePoint library (app token) ---
        from ..services.graph_app_auth import get_app_graph_token

        token = await get_app_graph_token()
        if not token:
            raise HTTPException(502, "Couldn't obtain app Graph token for company library")

        safe = _safe_name(file_name)
        url = f"{_GRAPH}/drives/{drive_id}/root:/Attachments/{entity_label}/{entity_id}/{safe}:/content"
        from ..http_client import http

        try:
            r = await http.put(
                url,
                content=content,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": content_type or "application/octet-stream",
                },
                timeout=60,
            )
        except Exception:
            logger.error("company-library attachment PUT errored url={}", url, exc_info=True)
            raise HTTPException(502, "Couldn't save to the company library")
        if r.status_code not in (200, 201):
            logger.error(
                "company-library attachment PUT failed {} {} url={}",
                r.status_code,
                r.text[:300],
                url,
            )
            raise HTTPException(502, "Couldn't save to the company library")
        try:
            body = r.json()
        except Exception:
            logger.error("attachment PUT returned non-JSON body url={}", url, exc_info=True)
            raise HTTPException(502, "Couldn't save to the company library")
        item_id = body.get("id")
        if not item_id:
            logger.error("attachment PUT response missing 'id' url={}", url)
            raise HTTPException(502, "Couldn't save to the company library")
        return item_id, drive_id, body.get("webUrl")

    # --- User OneDrive fallback ---
    from ..scheduler import get_valid_token

    token = await get_valid_token(user, db)
    if not token:
        raise HTTPException(401, "Connect your Microsoft account to attach files")

    safe = _safe_name(file_name)
    url = f"{_GRAPH}/me/drive/root:/AvailAI/{entity_label}/{entity_id}/{safe}:/content"
    from ..http_client import http

    try:
        r = await http.put(
            url,
            content=content,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type or "application/octet-stream",
            },
            timeout=30,
        )
    except Exception:
        logger.error("OneDrive attachment PUT errored url={}", url, exc_info=True)
        raise HTTPException(502, "Failed to upload to OneDrive")
    if r.status_code == 401:
        raise HTTPException(401, "Microsoft token expired — please re-authenticate")
    if r.status_code == 403:
        raise HTTPException(403, "Access denied to OneDrive")
    if r.status_code not in (200, 201):
        logger.error("OneDrive attachment PUT failed {} {}", r.status_code, r.text[:300])
        raise HTTPException(502, "Failed to upload to OneDrive")
    try:
        body = r.json()
    except Exception:
        logger.error("attachment PUT returned non-JSON body url={}", url, exc_info=True)
        raise HTTPException(502, "Failed to upload to OneDrive")
    item_id = body.get("id")
    if not item_id:
        logger.error("attachment PUT response missing 'id' url={}", url)
        raise HTTPException(502, "Failed to upload to OneDrive")
    return item_id, None, body.get("webUrl")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def store_and_attach(
    db: Session,
    *,
    model,
    fk_field: str,
    entity_label: str,
    entity_id: int,
    file: UploadFile,
    user,
) -> object:
    """Validate, upload, persist and return an attachment row.

    model        — ORM model class (e.g. CompanyAttachment) fk_field     — attribute
    name of the FK column (e.g. "company_id") entity_label — human path segment used for
    cloud folder (e.g. "Companies") entity_id    — PK of the owning entity file —
    FastAPI UploadFile user         — authenticated User ORM object
    """
    content = await file.read()
    _validate(file, content)

    item_id, drive_id, web_url = await _store(
        content,
        content_type=file.content_type or "application/octet-stream",
        file_name=file.filename or "unnamed_file",
        entity_label=entity_label,
        entity_id=entity_id,
        user=user,
        db=db,
    )

    safe = _safe_name(file.filename or "unnamed_file")
    att = model(
        **{fk_field: entity_id},
        file_name=safe,
        library_item_id=item_id,
        library_drive_id=drive_id,
        library_web_url=web_url,
        content_type=file.content_type,
        size_bytes=len(content),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def serialize(a) -> dict:
    """Return a JSON-serializable dict for an attachment row.

    kind is "library" for company-SharePoint rows and "onedrive" for fallback rows —
    determined solely by whether library_drive_id is set.
    """
    return {
        "id": a.id,
        "file_name": a.file_name,
        "web_url": a.library_web_url,
        "content_type": a.content_type,
        "size_bytes": a.size_bytes,
        "uploaded_by": a.uploaded_by.name if a.uploaded_by else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "kind": "library" if a.library_drive_id else "onedrive",
    }


async def open_attachment(att, user) -> StreamingResponse | RedirectResponse:
    """Serve the attachment per its storage kind.

    company-library rows → in-app byte stream (app token) OneDrive rows        →
    redirect to webUrl (user's OneDrive)
    """
    if att.library_drive_id:
        from ..services.datasheet_library import fetch_datasheet_bytes

        data = await fetch_datasheet_bytes(att.library_drive_id, att.library_item_id)
        if data is None:
            logger.warning(
                "open_attachment: library fetch returned None att_id={} drive={} item={}",
                att.id,
                att.library_drive_id,
                att.library_item_id,
            )
            raise HTTPException(404, "Attachment file not found in library")
        safe = "".join(c for c in (att.file_name or "") if c.isalnum() or c in "._- ") or "file"
        return StreamingResponse(
            BytesIO(data),
            media_type=att.content_type or "application/octet-stream",
            headers={"Content-Disposition": f'inline; filename="{safe}"'},
        )

    # OneDrive fallback — redirect
    if not att.library_web_url:
        logger.warning("open_attachment: attachment has no URL att_id={}", att.id)
        raise HTTPException(404, "Attachment has no URL")
    return RedirectResponse(att.library_web_url)


async def remove_attachment(db: Session, att, user) -> dict:
    """Best-effort cloud delete then DB delete.

    On cloud-delete failure: DB row is still removed; response includes a
    warning key (mirrors existing requisition delete semantics).
    Never raises on cloud failure — the user's goal (remove the record) is met.
    """
    warning: str | None = None

    if att.library_drive_id and att.library_item_id:
        # Company library — app token DELETE
        try:
            from ..services.graph_app_auth import get_app_graph_token

            token = await get_app_graph_token()
            if token:
                from ..http_client import http

                url = f"{_GRAPH}/drives/{att.library_drive_id}/items/{att.library_item_id}"
                r = await http.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
                if r.status_code not in (200, 204):
                    logger.warning(
                        "company-library delete returned {} for item={}",
                        r.status_code,
                        att.library_item_id,
                    )
                    warning = "DB record deleted but cloud file may need manual cleanup"
            else:
                logger.warning("no app token — skipping cloud delete for item={}", att.library_item_id)
                warning = "DB record deleted but cloud file may need manual cleanup"
        except Exception:
            logger.warning("cloud delete errored item={}", att.library_item_id, exc_info=True)
            warning = "DB record deleted but cloud file may need manual cleanup"

    elif att.library_item_id:
        # OneDrive fallback — user token DELETE
        try:
            from ..scheduler import get_valid_token

            token = await get_valid_token(user, db)
            if token:
                from ..http_client import http

                url = f"{_GRAPH}/me/drive/items/{att.library_item_id}"
                r = await http.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
                if r.status_code not in (200, 204):
                    logger.warning(
                        "OneDrive delete returned {} for item={}",
                        r.status_code,
                        att.library_item_id,
                    )
                    warning = "DB record deleted but cloud file may need manual cleanup"
            else:
                logger.warning("no user token — skipping OneDrive delete item={}", att.library_item_id)
                warning = "DB record deleted but cloud file may need manual cleanup"
        except Exception:
            logger.warning("OneDrive delete errored item={}", att.library_item_id, exc_info=True)
            warning = "DB record deleted but cloud file may need manual cleanup"

    db.delete(att)
    db.commit()
    result: dict = {"ok": True}
    if warning:
        result["warning"] = warning
    return result

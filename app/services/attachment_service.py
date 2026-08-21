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
from typing import TYPE_CHECKING

from fastapi import HTTPException, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import ALLOWED_ATTACHMENT_EXTENSIONS, MAX_ATTACHMENT_BYTES

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

_GRAPH = "https://graph.microsoft.com/v1.0"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _safe_name(name: str) -> str:
    """Sanitize a filename for use as a Graph path segment."""
    return (name or "unnamed_file").replace("/", "_").replace("\\", "_")


# Trusted content type derived from the (validated) extension — the client's
# UploadFile.content_type is attacker-controlled and must never drive serving
# (QC 2026-08-08 stored-XSS fix). Only images + PDF are safe to render inline;
# everything else is forced to download.
_EXT_CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".zip": "application/zip",
}
_INLINE_SAFE_TYPES = {"application/pdf", "image/png", "image/jpeg"}


def _trusted_content_type(file_name: str | None) -> str:
    """Content type from the file extension, never the client-supplied header."""
    ext = os.path.splitext(file_name or "")[1].lower()
    return _EXT_CONTENT_TYPE.get(ext, "application/octet-stream")


_READ_CHUNK = 64 * 1024  # 64 KB per read while capping the upload


async def _read_capped(file: UploadFile) -> bytes:
    """Read an UploadFile into memory, aborting the moment it exceeds the size cap.

    QC 2026-08-13: the prior ``await file.read()`` buffered the ENTIRE request body
    before ``_validate`` ever checked the size, so concurrent oversized uploads
    could OOM the container (there is no body-size limit in front of the app).
    """
    buf = bytearray()
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(400, "File too large (max 10 MB)")
    return bytes(buf)


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


async def _graph_put(
    url: str,
    token: str,
    content: bytes,
    *,
    content_type: str,
    timeout: int,
    err_msg: str,
    label: str,
    map_status=None,
) -> tuple[str, str | None]:
    """Shared Graph file-upload PUT for both storage backends.

    Performs the request, checks the status, parses the JSON body, and extracts the item
    id — every failure raises HTTPException(502, *err_msg*) after an error log prefixed
    with *label*. An optional *map_status* hook is called with the response BEFORE the
    generic status check so a backend can map specific codes to its own HTTP errors (the
    OneDrive 401/403 mapping stays at its call site).

    Returns (item_id, web_url).
    """
    from ..http_client import http

    try:
        r = await http.put(
            url,
            content=content,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type or "application/octet-stream",
            },
            timeout=timeout,
        )
    except Exception as e:
        logger.error("{} attachment PUT errored url={}", label, url, exc_info=True)
        raise HTTPException(502, err_msg) from e
    if map_status is not None:
        map_status(r)
    if r.status_code not in (200, 201):
        logger.error("{} attachment PUT failed {} {} url={}", label, r.status_code, r.text[:300], url)
        raise HTTPException(502, err_msg)
    try:
        body = r.json()
    except Exception as e:
        logger.error("attachment PUT returned non-JSON body url={}", url, exc_info=True)
        raise HTTPException(502, err_msg) from e
    item_id = body.get("id")
    if not item_id:
        logger.error("attachment PUT response missing 'id' url={}", url)
        raise HTTPException(502, err_msg)
    return item_id, body.get("webUrl")


def _map_onedrive_status(r) -> None:
    """OneDrive-only PUT status mapping: auth failures get their own HTTP errors
    instead of the generic 502."""
    if r.status_code == 401:
        raise HTTPException(401, "Microsoft token expired — please re-authenticate")
    if r.status_code == 403:
        raise HTTPException(403, "Access denied to OneDrive")


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
    safe = _safe_name(file_name)

    if drive_id:
        # --- Company SharePoint library (app token) ---
        from ..services.graph_app_auth import get_app_graph_token

        token = await get_app_graph_token()
        if not token:
            raise HTTPException(502, "Couldn't obtain app Graph token for company library")

        url = f"{_GRAPH}/drives/{drive_id}/root:/Attachments/{entity_label}/{entity_id}/{safe}:/content"
        item_id, web_url = await _graph_put(
            url,
            token,
            content,
            content_type=content_type,
            timeout=60,
            err_msg="Couldn't save to the company library",
            label="company-library",
        )
        return item_id, drive_id, web_url

    # --- User OneDrive fallback ---
    from ..scheduler import get_valid_token

    token = await get_valid_token(user, db)
    if not token:
        raise HTTPException(401, "Connect your Microsoft account to attach files")

    url = f"{_GRAPH}/me/drive/root:/AvailAI/{entity_label}/{entity_id}/{safe}:/content"
    item_id, web_url = await _graph_put(
        url,
        token,
        content,
        content_type=content_type,
        timeout=30,
        err_msg="Failed to upload to OneDrive",
        label="OneDrive",
        map_status=_map_onedrive_status,
    )
    return item_id, None, web_url


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
    content = await _read_capped(file)
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
        # Store the extension-derived type, NOT the attacker-controlled client
        # header — the stored value drives inline serving (QC 2026-08-08).
        content_type=_trusted_content_type(safe),
        size_bytes=len(content),
        uploaded_by_id=user.id,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


# Per-kind delete URL stems — the DELETE endpoint family for each entity.
# Used by the HX-Request list partial so each row's delete button targets the
# right route. Kept here (next to serialize) as the single source of truth.
_DELETE_BASE: dict[str, str] = {
    "requisition": "/api/requisition-attachments",
    "requirement": "/api/requirement-attachments",
    "offer": "/api/offer-attachments",
    "company": "/api/company-attachments",
    "contact": "/api/contact-attachments",
    "material": "/api/material-card-attachments",
    "vendor_card": "/api/vendor-attachments",
    "vendor_contact": "/api/vendor-contact-attachments",
}


def attachment_list_response(
    request: Request | None, *, kind: str, entity_id: int, rows: list
) -> Response | list[dict]:
    """Return the attachment list as HTML (HTMX) or JSON (back-compat).

    When the request carries `HX-Request`, render the shared list partial so the
    panel is HTMX-native; otherwise return the plain JSON array the existing API
    callers (and tests) expect.

    request   — Starlette/FastAPI Request
    kind      — attachment kind (requisition|requirement|offer|company|contact|material)
    entity_id — owning entity PK (for the partial's symmetry)
    rows      — ORM attachment rows (serialized here)
    """
    items = [serialize(a) for a in rows]
    if request is not None and request.headers.get("HX-Request"):
        from ..template_env import template_response

        return template_response(
            "htmx/partials/shared/_attachment_list.html",
            {
                "request": request,
                "kind": kind,
                "entity_id": entity_id,
                "items": items,
                "delete_base": _DELETE_BASE[kind],
            },
        )
    return items


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
        # Re-derive from the filename so a legacy row carrying a spoofed stored
        # content_type can't render inline either; only images + PDF inline, and
        # nosniff blocks MIME-sniffing an HTML payload out of a .txt/.csv/.zip
        # (QC 2026-08-08 stored-XSS fix).
        media_type = _trusted_content_type(att.file_name)
        disposition = "inline" if media_type in _INLINE_SAFE_TYPES else "attachment"
        return StreamingResponse(
            BytesIO(data),
            media_type=media_type,
            headers={
                "Content-Disposition": f'{disposition}; filename="{safe}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    # OneDrive fallback — redirect
    if not att.library_web_url:
        logger.warning("open_attachment: attachment has no URL att_id={}", att.id)
        raise HTTPException(404, "Attachment has no URL")
    return RedirectResponse(att.library_web_url)


# The single warning string for every best-effort cloud-delete failure path.
_CLOUD_DELETE_WARNING = "DB record deleted but cloud file may need manual cleanup"


async def _graph_delete(url: str, token: str, item_id, label: str) -> bool:
    """Shared Graph item DELETE for both storage backends.

    Returns True on 200/204; logs a *label*-prefixed warning and returns False on any
    other status. Exceptions propagate — each call site owns its try/except (the whole
    delete, token fetch included, is best-effort there).
    """
    from ..http_client import http

    r = await http.delete(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if r.status_code not in (200, 204):
        logger.warning("{} delete returned {} for item={}", label, r.status_code, item_id)
        return False
    return True


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
                url = f"{_GRAPH}/drives/{att.library_drive_id}/items/{att.library_item_id}"
                if not await _graph_delete(url, token, att.library_item_id, "company-library"):
                    warning = _CLOUD_DELETE_WARNING
            else:
                logger.warning("no app token — skipping cloud delete for item={}", att.library_item_id)
                warning = _CLOUD_DELETE_WARNING
        except Exception:
            logger.warning("cloud delete errored item={}", att.library_item_id, exc_info=True)
            warning = _CLOUD_DELETE_WARNING

    elif att.library_item_id:
        # OneDrive fallback — user token DELETE
        try:
            from ..scheduler import get_valid_token

            token = await get_valid_token(user, db)
            if token:
                url = f"{_GRAPH}/me/drive/items/{att.library_item_id}"
                if not await _graph_delete(url, token, att.library_item_id, "OneDrive"):
                    warning = _CLOUD_DELETE_WARNING
            else:
                logger.warning("no user token — skipping OneDrive delete item={}", att.library_item_id)
                warning = _CLOUD_DELETE_WARNING
        except Exception:
            logger.warning("OneDrive delete errored item={}", att.library_item_id, exc_info=True)
            warning = _CLOUD_DELETE_WARNING

    db.delete(att)
    db.commit()
    result: dict = {"ok": True}
    if warning:
        result["warning"] = warning
    return result

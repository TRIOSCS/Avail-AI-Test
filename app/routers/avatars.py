"""User profile-avatar upload + serving API.

Profile photos are written to ``AVATARS_DIR`` on the ``uploads`` Docker named
volume — the same volume + ownership pattern as trouble-ticket screenshots
(``error_reports.UPLOAD_DIR``). A parallel ``ensure_avatar_storage()`` guard in
startup.py and an entrypoint chown keep the dir writable by the non-root app
process on every boot.

Endpoints (all gated to the logged-in user editing their OWN profile — the
current user is always ``require_user``; there is no path param to act on
another user, so own-profile-only is structural):
  POST   /api/user/avatar            — upload/replace the current user's photo
  DELETE /api/user/avatar            — clear the current user's photo
  GET    /api/user/avatar/{filename} — serve a stored avatar (login-gated)

Called by: main.py (app.include_router), settings/profile.html (uploader),
           shared/_macros.html user_avatar macro (img src)
Depends on: models/auth.py (User.avatar_path), startup.py (ensure_avatar_storage)
"""

import asyncio
import json
import os
import uuid

import filetype
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..database import get_db
from ..dependencies import require_user
from ..models import User

router = APIRouter(tags=["avatars"])


def _avatar_response(toast: str, filename: str | None) -> HTMLResponse:
    """Empty 200 that both refreshes the avatar UI and shows a settings toast.

    HTMX merges all events into ONE HX-Trigger JSON object, so the avatarUpdated refresh
    (carrying the new basename, or null when cleared) and the showToast feedback share a
    single header.
    """
    trigger = json.dumps(
        {
            "avatarUpdated": {"filename": filename},
            "showToast": {"message": toast, "type": "success"},
        }
    )
    return HTMLResponse(status_code=200, headers={"HX-Trigger": trigger})


AVATARS_DIR = "/app/uploads/avatars"
MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB
# Real (magic-byte-detected) mime → file extension. The upload route derives both the
# accepted type AND the on-disk extension from the VERIFIED bytes via filetype.guess(),
# never from the attacker-controlled Content-Type header; the serve route infers media
# type back from the extension. This map is the single source of truth for accepted
# image types.
ALLOWED_AVATAR_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_EXT_MEDIA_TYPE = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

_avatar_dir_ready = False


def _ensure_avatar_dir() -> None:
    global _avatar_dir_ready
    if not _avatar_dir_ready:
        os.makedirs(AVATARS_DIR, exist_ok=True)
        _avatar_dir_ready = True


def _write_avatar_file(path: str, data: bytes) -> None:
    """Sync file write — always dispatched via ``asyncio.to_thread`` (P2.6) so a
    slow/contended disk doesn't block the event loop for other requests."""
    with open(path, "wb") as f:
        f.write(data)


def _json_error(request: Request, status_code: int, message: str) -> JSONResponse:
    req_id = getattr(request.state, "request_id", "unknown")
    return JSONResponse(
        status_code=status_code,
        content={"error": message, "status_code": status_code, "request_id": req_id},
    )


@router.post("/api/user/avatar", response_class=HTMLResponse)
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Store an uploaded image as the current user's profile photo.

    Validates the REAL image type by magic bytes (PNG/JPEG/WEBP/GIF) and size
    (≤2 MB), writes the file under AVATARS_DIR with a per-user UUID basename, deletes
    any prior avatar, and sets ``user.avatar_path``. The accepted type AND the on-disk
    extension are a function of the verified bytes — never the attacker-controlled
    ``Content-Type`` header — so a polyglot labelled ``image/png`` cannot be stored as
    ``.png`` and served back inline. Own-profile only — there is no user path param, so
    a logged-in user can only ever change their own photo.
    """
    data = await file.read()
    if not data:
        return _json_error(request, 400, "Uploaded avatar is empty.")
    if len(data) > MAX_AVATAR_BYTES:
        return _json_error(request, 400, "Avatar must be 2 MB or smaller.")

    # Trust the bytes, not the header: filetype.guess inspects magic bytes, so the
    # accepted mime (and the extension written to disk) is derived from real content.
    kind = filetype.guess(data)
    if kind is None or kind.mime not in ALLOWED_AVATAR_TYPES:
        return _json_error(request, 400, "Avatar must be a PNG, JPEG, WEBP, or GIF image.")
    ext = ALLOWED_AVATAR_TYPES[kind.mime]

    _ensure_avatar_dir()
    filename = f"user_{user.id}_{uuid.uuid4().hex[:8]}.{ext}"
    path = os.path.join(AVATARS_DIR, filename)
    try:
        await asyncio.to_thread(_write_avatar_file, path, data)
    except OSError as e:
        logger.error("Avatar write failed for user {}: {}", user.id, e)
        return _json_error(request, 500, "Avatar storage is not writable. Contact support.")

    # Remove the prior file so old avatars don't accumulate on the volume.
    _delete_avatar_file(user.avatar_path)

    user.avatar_path = filename
    db.commit()
    logger.info("Avatar uploaded", user_id=user.id, filename=filename)
    return _avatar_response("Profile photo updated.", filename)


@router.delete("/api/user/avatar", response_class=HTMLResponse)
async def delete_avatar(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Clear the current user's profile photo (revert to the initials fallback)."""
    _delete_avatar_file(user.avatar_path)
    user.avatar_path = None
    db.commit()
    logger.info("Avatar removed", user_id=user.id)
    return _avatar_response("Profile photo removed.", None)


@router.get("/api/user/avatar/{filename}")
async def serve_avatar(
    filename: str,
    user: User = Depends(require_user),
):
    """Serve a stored avatar image from disk (any logged-in user may view it).

    Path-traversal guarded the same way as the screenshot serve route: the
    resolved real path must live under AVATARS_DIR.
    """
    real_path = os.path.realpath(os.path.join(AVATARS_DIR, filename))
    if not real_path.startswith(os.path.realpath(AVATARS_DIR) + os.sep):
        logger.warning("Avatar path traversal blocked: {}", filename)
        raise HTTPException(403, "Invalid avatar path")
    if not os.path.isfile(real_path):
        raise HTTPException(404, "Avatar not found")
    ext = real_path.rsplit(".", 1)[-1].lower()
    return FileResponse(real_path, media_type=_EXT_MEDIA_TYPE.get(ext, "application/octet-stream"))


def _delete_avatar_file(filename: str | None) -> None:
    """Best-effort delete of a stored avatar basename (path-traversal guarded)."""
    if not filename:
        return
    real_path = os.path.realpath(os.path.join(AVATARS_DIR, filename))
    if not real_path.startswith(os.path.realpath(AVATARS_DIR) + os.sep):
        return
    try:
        os.remove(real_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not remove old avatar {}: {}", filename, e)

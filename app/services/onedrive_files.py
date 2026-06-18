"""onedrive_files.py — reusable OneDrive (Graph) byte upload for background jobs.

Extracted from the requisition/offer attachment upload pattern so non-request code
(e.g. the datasheet capture job) can store a file in the user's OneDrive. Delegated
token via get_valid_token; uploads to /me/drive/root:/<folder_path>/<file_name>:/content.
"""

from __future__ import annotations

from loguru import logger

from ..http_client import http
from ..utils.token_manager import get_valid_token


async def upload_bytes_to_onedrive(
    user, db, folder_path: str, file_name: str, content: bytes, content_type: str
) -> dict | None:
    """Upload bytes to OneDrive; return {onedrive_item_id, onedrive_url, size_bytes} or
    None."""
    token = await get_valid_token(user, db)
    if not token:
        logger.warning("onedrive upload skipped — no Graph token for user")
        return None
    safe_name = (file_name or "file").replace("/", "_").replace("\\", "_")
    drive_path = f"/me/drive/root:/{folder_path}/{safe_name}:/content"
    try:
        resp = await http.put(
            f"https://graph.microsoft.com/v1.0{drive_path}",
            content=content,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type or "application/octet-stream"},
            timeout=60,
        )
    except Exception:
        logger.warning("onedrive upload errored path={}", drive_path, exc_info=True)
        return None
    if resp.status_code not in (200, 201):
        logger.warning("onedrive upload failed {} {}", resp.status_code, resp.text[:200])
        return None
    result = resp.json()
    return {
        "onedrive_item_id": result.get("id"),
        "onedrive_url": result.get("webUrl"),
        "size_bytes": len(content),
    }

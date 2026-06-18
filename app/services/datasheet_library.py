"""datasheet_library.py — store/fetch datasheets in the company SharePoint library.

App-only Graph (get_app_graph_token) against the configured library drive
(settings.datasheet_library_drive_id). Unconfigured or no-token => returns None so the
caller skips storage gracefully. Separate from onedrive_files (per-user req/offer
attachments).
"""

from __future__ import annotations

import re

from loguru import logger

from ..config import settings
from ..http_client import http, http_redirect
from .graph_app_auth import get_app_graph_token

_GRAPH = "https://graph.microsoft.com/v1.0"


def _sanitize(part: str) -> str:
    return re.sub(r"[\\/]+", "_", (part or "").strip()) or "_unknown"


async def upload_datasheet_to_library(
    file_name: str, content: bytes, content_type: str, *, manufacturer: str = ""
) -> dict | None:
    """PUT the bytes into the company library; return metadata dict or None."""
    drive_id = settings.datasheet_library_drive_id
    if not drive_id:
        logger.info("datasheet library not configured — skipping storage")
        return None
    token = await get_app_graph_token()
    if not token:
        logger.warning("no app Graph token — skipping datasheet storage")
        return None
    folder = f"{settings.datasheet_library_subpath}/{_sanitize(manufacturer)}"
    safe_name = _sanitize(file_name)
    url = f"{_GRAPH}/drives/{drive_id}/root:/{folder}/{safe_name}:/content"
    try:
        r = await http.put(
            url,
            content=content,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type or "application/octet-stream"},
            timeout=60,
        )
    except Exception:
        logger.warning("datasheet library upload errored url={}", url, exc_info=True)
        return None
    if r.status_code not in (200, 201):
        logger.warning("datasheet library upload failed {} {}", r.status_code, r.text[:200])
        return None
    body = r.json()
    return {
        "onedrive_item_id": body.get("id"),
        "onedrive_url": body.get("webUrl"),
        "size_bytes": len(content),
        "library_drive_id": drive_id,
    }


async def fetch_datasheet_bytes(drive_id: str, item_id: str) -> bytes | None:
    """GET the item content from the library (app-only).

    Returns bytes or None.
    """
    if not (drive_id and item_id):
        return None
    token = await get_app_graph_token()
    if not token:
        return None
    url = f"{_GRAPH}/drives/{drive_id}/items/{item_id}/content"
    try:
        r = await http_redirect.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    except Exception:
        logger.warning("datasheet library fetch errored item={}", item_id, exc_info=True)
        return None
    if r.status_code != 200 or not r.content:
        return None
    return r.content

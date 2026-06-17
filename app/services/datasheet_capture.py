"""datasheet_capture.py — find/download/verify/store a part's datasheet.

Primitives here; the capture orchestrator (capture_datasheet) is added in Task 6.
"""

from __future__ import annotations

import io
import re

from loguru import logger

from ..http_client import http_redirect
from ..utils.normalization import normalize_mpn_key

MAX_DATASHEET_BYTES = 25 * 1024 * 1024
_MAX_VERIFY_PAGES = 20
_NONALNUM = re.compile(r"[^a-z0-9]")


async def download_pdf(url: str) -> bytes | None:
    """GET a URL (following redirects); return bytes iff it is a PDF within the size
    cap."""
    if not url:
        return None
    try:
        resp = await http_redirect.get(url, timeout=60)
    except Exception:
        logger.warning("datasheet download errored url={}", url, exc_info=True)
        return None
    if resp.status_code != 200:
        return None
    content = resp.content
    if not content or len(content) > MAX_DATASHEET_BYTES:
        return None
    ctype = (resp.headers.get("content-type") or "").lower()
    if not (content[:5] == b"%PDF-" or "application/pdf" in ctype):
        return None
    return content


def pdf_contains_mpn(pdf_bytes: bytes, mpn: str) -> bool:
    """True if the MPN (normalized key, len>=4) appears in the PDF's extracted text."""
    key = normalize_mpn_key(mpn)
    if len(key) < 4:
        return False
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        text_parts = []
        for page in reader.pages[:_MAX_VERIFY_PAGES]:
            text_parts.append(page.extract_text() or "")
        text_key = _NONALNUM.sub("", "".join(text_parts).lower())
    except Exception:
        logger.warning("datasheet pdf parse failed", exc_info=True)
        return False
    return key in text_key

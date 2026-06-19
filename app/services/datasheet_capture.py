"""datasheet_capture.py — find/download/verify/store a part's datasheet.

Primitives (download_pdf, pdf_contains_mpn) plus finder + capture orchestrator.
"""

from __future__ import annotations

import io
import ipaddress
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from loguru import logger

from ..database import SessionLocal
from ..http_client import http
from ..utils.normalization import normalize_mpn_key
from .datasheet_library import upload_datasheet_to_library

MAX_DATASHEET_BYTES = 25 * 1024 * 1024
_MAX_VERIFY_PAGES = 20
_MAX_REDIRECTS = 5
_NONALNUM = re.compile(r"[^a-z0-9]")


def _is_safe_url(url: str) -> bool:
    """Reject non-http(s) schemes and any host that resolves to a private/loopback/
    link-local/multicast/reserved/unspecified address (SSRF guard).

    Note: this does a
    DNS resolution check; a determined DNS-rebinding attacker could still race it, but
    it blocks the realistic vectors (direct-internal URL, redirect-to-internal).
    """
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except Exception:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


async def download_pdf(url: str) -> bytes | None:
    """GET a URL with per-hop SSRF validation (no blind redirect following); return
    bytes iff it is a PDF within the size cap."""
    if not url:
        return None
    import asyncio

    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not await asyncio.to_thread(_is_safe_url, current):
            logger.warning("datasheet download blocked unsafe url={}", current)
            return None
        try:
            resp = await http.get(current, timeout=60)
        except Exception:
            logger.warning("datasheet download errored url={}", current, exc_info=True)
            return None
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                return None
            current = urljoin(current, location)
            continue
        if resp.status_code != 200:
            return None
        content = resp.content
        if not content or len(content) > MAX_DATASHEET_BYTES:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if not (content[:5] == b"%PDF-" or "application/pdf" in ctype):
            return None
        return content
    logger.warning("datasheet download exceeded max redirects url={}", url)
    return None


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


# ── Finder + capture orchestrator (Task 6) ──────────────────────────────────

CAPTURE_COOLDOWN_DAYS = 30


def _load_user(db, user_id: int):
    from ..models import User

    return db.query(User).filter(User.id == user_id).first()


async def find_datasheet_url(card, mpn: str) -> tuple[str, str] | None:
    """Connector datasheet_url first (trusted); else Claude web_search (untrusted)."""
    if card is not None and card.datasheet_url:
        return (card.datasheet_url, "connector")

    if os.environ.get("TESTING"):
        return None
    from .credential_service import get_credential_cached

    if not get_credential_cached("anthropic_ai", "ANTHROPIC_API_KEY"):
        return None
    from ..utils.claude_client import claude_json

    mfr = (getattr(card, "manufacturer", "") or "") if card else ""
    prompt = (
        f"Find the official manufacturer datasheet PDF for part number '{mpn}'"
        f'{(" by " + mfr) if mfr else ""}. Return JSON {{"datasheet_url": "<direct PDF url>"}} '
        f'or {{"datasheet_url": null}} if none found. The URL must point directly at a PDF.'
    )
    try:
        out = await claude_json(
            prompt,
            model_tier="smart",
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            timeout=60,
            cost_bucket="datasheet_capture",
        )
    except Exception:
        logger.warning("datasheet web_search failed mpn={}", mpn, exc_info=True)
        return None
    url = (out or {}).get("datasheet_url") if isinstance(out, dict) else None
    return (url, "web") if url else None


async def capture_datasheet(mpn: str, user_id: int) -> None:
    """Fire-and-forget: find → verify → store a datasheet copy on the MPN's card.

    Opens its own session (request session is gone by the time this runs).
    """
    from ..models import MaterialCard, MaterialCardDatasheet

    db = SessionLocal()
    try:
        key = normalize_mpn_key(mpn)
        if not key:
            return
        card = (
            db.query(MaterialCard).filter(MaterialCard.normalized_mpn == key, MaterialCard.deleted_at.is_(None)).first()
        )
        # Gate: already stored, or within negative-cache cooldown.
        if card is not None:
            if card.datasheets:
                return
            if card.datasheet_searched_at:
                age = datetime.now(timezone.utc) - _as_utc(card.datasheet_searched_at)
                if age < timedelta(days=CAPTURE_COOLDOWN_DAYS):
                    return

        found = await find_datasheet_url(card, mpn)
        if not found:
            _stamp_searched(db, card)
            return
        url, source = found

        pdf = await download_pdf(url)
        if not pdf:
            _stamp_searched(db, card)
            return

        if source == "web" and not pdf_contains_mpn(pdf, mpn):
            _stamp_searched(db, card)  # wrong file — do not store
            return

        # Ensure a card exists to attach to (approved cardless rule: verified hit only).
        if card is None:
            from ..search_service import resolve_material_card

            card = resolve_material_card(mpn, db)
            if card is None:
                return

        user = _load_user(db, user_id)  # optional attribution; storage no longer needs a user token
        meta = await upload_datasheet_to_library(
            f"{card.display_mpn}-datasheet.pdf", pdf, "application/pdf", manufacturer=card.manufacturer or ""
        )
        if not meta:
            _stamp_searched(db, card)
            return

        now = datetime.now(timezone.utc)
        db.add(
            MaterialCardDatasheet(
                material_card_id=card.id,
                file_name=f"{card.display_mpn}-datasheet.pdf",
                onedrive_item_id=meta["onedrive_item_id"],
                onedrive_url=meta["onedrive_url"],
                library_drive_id=meta["library_drive_id"],
                content_type="application/pdf",
                size_bytes=meta["size_bytes"],
                source=source,
                original_url=url,
                verified=True,
                uploaded_by_id=user.id if user is not None else None,
                captured_at=now,
            )
        )
        card.datasheet_captured_at = now
        db.commit()
        logger.info("datasheet captured mpn={} source={}", mpn, source)
    except Exception:
        logger.exception("capture_datasheet failed mpn={}", mpn)
        db.rollback()
    finally:
        db.close()


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stamp_searched(db, card) -> None:
    if card is None:
        return  # cardless miss — no place to negative-cache (re-hunts next trigger)
    card.datasheet_searched_at = datetime.now(timezone.utc)
    db.commit()

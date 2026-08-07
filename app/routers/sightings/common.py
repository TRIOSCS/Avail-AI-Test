"""Shared state for the sightings package — router, cache, toasts, SSE, shared panels.

W4.1 split of the 3,811-line app/routers/sightings.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import json
import time
from typing import Any, Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...constants import (
    RequisitionStatus,
    SourcingStatus,
)
from ...models.intelligence import MaterialCard
from ...models.sourcing import Requirement
from ...models.vendors import VendorContact
from ...services.part_offers import part_offers_for
from ...services.sse_broker import broker
from ...template_env import template_response

router = APIRouter(tags=["sightings"])

MAX_BATCH_SIZE: Final[int] = 50
# Cap on concurrent background search_requirement() fan-outs so a bulk refresh of up to
# MAX_BATCH_SIZE requirements does not stampede the supplier APIs all at once.
_SEARCH_FANOUT_LIMIT: Final[int] = 5
# Requisitions in these statuses are excluded from sightings (no active sourcing).
# WON/LOST/CANCELLED are terminal deals — the sightings board is for buyers to ACTIVELY
# source OPEN work, so closed deals never appear.
_EXCLUDED_REQ_STATUSES: Final = (
    RequisitionStatus.CANCELLED,
    RequisitionStatus.WON,
    RequisitionStatus.LOST,
)
# Per-part terminal states — a closed requirement (won/lost/archived) drops off the active
# sourcing board even when its parent requisition is still open (multi-part deals).
_EXCLUDED_SOURCING_STATUSES: Final = (
    SourcingStatus.WON,
    SourcingStatus.LOST,
    SourcingStatus.ARCHIVED,
)


def _active_sourcing_status_clause():
    """Sourcing-status predicate that keeps a NULL status on the active board.

    ``Requirement.sourcing_status`` is nullable — ``default="open"`` is a CLIENT-side
    default only (no ``server_default``/NOT NULL), so a row can legitimately be NULL. A
    bare ``sourcing_status.notin_(_EXCLUDED_SOURCING_STATUSES)`` compiles to ``NULL NOT IN
    (...)`` which SQL evaluates to NULL (falsy), silently dropping an active NULL-status
    requirement from the board, its facet counts, the urgent/stale tiles, and the
    cross-requirement vendor-overlap query. Coalescing NULL to "active" here treats such a
    part as open (matching the pre-change behavior). Shared by every active-only query so
    they stay in lockstep.
    """
    return or_(
        Requirement.sourcing_status.is_(None),
        Requirement.sourcing_status.notin_(_EXCLUDED_SOURCING_STATUSES),
    )


_cache: dict[str, tuple[float, Any]] = {}


def _get_cached(key: str, ttl: float, factory):
    """Simple in-process TTL cache.

    For value tuples/dicts only (not ORM objects). Safe because cached results are
    detached column tuples, not session-bound objects.
    """
    now = time.monotonic()
    entry = _cache.get(key)
    if entry and now - entry[0] < ttl:
        return entry[1]
    result = factory()
    _cache[key] = (now, result)
    return result


def _invalidate_cache(key: str):
    """Remove a cached entry (call after mutations that change the data)."""
    _cache.pop(key, None)


def _oob_toast_html(msg: str, level: str = "success") -> str:
    """The OOB toast fragment — swaps into #toast-trigger and fires $store.toast.

    msg is embedded in a single-quoted JS string inside the x-init attribute. Escape
    ``&`` FIRST: the browser HTML-decodes the attribute before Alpine evaluates the JS,
    so an injected entity like ``&#39;`` would otherwise decode into a real quote and
    break out of the string. Then backslash (so a ``\\'`` payload can't escape the
    escaper's own backslash), then the single quote (JS string), then the double quote
    (the surrounding attribute). ``level`` is a server-controlled constant.
    """
    safe_msg = msg.replace("&", "&amp;").replace("\\", "\\\\").replace("'", "\\'").replace('"', "&quot;")
    return (
        f'<div hx-swap-oob="true" id="toast-trigger"'
        f" x-init=\"$store.toast.message='{safe_msg}';"
        f"$store.toast.type='{level}';"
        f'$store.toast.show=true"></div>'
    )


def _oob_toast(msg: str, level: str = "success") -> HTMLResponse:
    """Return an OOB swap div that triggers a toast notification via Alpine."""
    return HTMLResponse(_oob_toast_html(msg, level))


def _append_oob_toast(resp: Response, msg: str, level: str = "success") -> HTMLResponse:
    """Append the OOB toast fragment to an already-rendered HTMX response (mark/clear
    feedback on detail re-renders), preserving the original custom headers."""
    out = HTMLResponse(resp.body.decode("utf-8") + _oob_toast_html(msg, level), status_code=resp.status_code)
    for key, value in resp.headers.items():
        if key.lower() not in ("content-length", "content-type"):
            out.headers[key] = value
    return out


def _render_offers_panel(request: Request, requirement: Requirement, db: Session) -> HTMLResponse:
    """Render the part-centric Offers panel for swap into #sightings-offers-panel."""
    ctx = {
        "request": request,
        "requirement": requirement,
        "part_offers": part_offers_for(requirement, db),
    }
    resp = template_response("htmx/partials/sightings/offers_panel.html", ctx)
    resp.headers["X-Rendered-Req-Id"] = str(requirement.id)
    return resp


def _with_toast(resp: HTMLResponse, msg: str, level: str = "success") -> HTMLResponse:
    """Attach the `showToast` HX-Trigger to an HTMX response (the same toast trigger
    this router already emits via HX-Trigger elsewhere)."""
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": msg, "type": level}})
    return resp


async def _publish_if_user_source(source: str, user_id: int, requirement_id: int) -> None:
    """Publish sighting-updated SSE only when the caller is a human user.

    Skips publish when source == 'sse' to prevent self-trigger loops.
    """
    if source != "sse":
        await broker.publish(
            f"user:{user_id}",
            "sighting-updated",
            json.dumps({"requirement_id": requirement_id}),
        )


def _toast_suppressed_for_sse(source: str) -> bool:
    """Return True when the caller is an SSE-triggered request."""
    return source == "sse"


def _mpn_link_map(db: Session, requirements) -> dict[str, int]:
    """Build a display-MPN → MaterialCard.id map for the given requirements.

    Collects each requirement's primary_mpn plus substitute MPNs (string or dict form),
    normalizes them, and resolves live (non-deleted) MaterialCards in one query. Shared
    by sightings_list (table) and sightings_detail (header). Empty input → empty map.
    """
    from ...utils.normalization import normalize_mpn_key

    all_mpns: set[str] = set()
    for r in requirements:
        if r.primary_mpn:
            all_mpns.add(r.primary_mpn.upper())
        for sub in r.substitutes or []:
            mpn = sub.get("mpn") if isinstance(sub, dict) else sub
            if mpn:
                all_mpns.add(str(mpn).upper())
    if not all_mpns:
        return {}

    norm_to_display: dict[str, str] = {}
    for mpn in all_mpns:
        n = normalize_mpn_key(mpn)
        if n:
            norm_to_display[n] = mpn

    cards = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn)
        .filter(
            MaterialCard.normalized_mpn.in_(list(norm_to_display.keys())),
            MaterialCard.deleted_at.is_(None),
        )
        .all()
    )
    link_map: dict[str, int] = {}
    for card_id, norm in cards:
        display = norm_to_display.get(norm)
        if display:
            link_map[display] = card_id
    return link_map


def _best_contacts_by_card(db: Session, card_ids: list[int]) -> list[VendorContact]:
    """Vendor contacts ordered worst-first so a last-wins ``{card_id: c}`` dict keeps
    the BEST contact per vendor.

    VendorContact has no is_primary flag, and a vendor can hold several contacts (an
    rfq_manual row added inline via the composer alongside an enriched row). An
    unordered ``{c.vendor_card_id: c for c in contacts}`` lets an arbitrary (possibly
    EMPTY-email) row win, which would silently skip the vendor as "had no email". Ordering
    a usable email LAST (then verified, then higher confidence) makes the real email win.

    An EMPTY-OR-NULL email row sorts FIRST (loses last-wins) — both ``NULL`` and ``''`` are
    unusable (the send path only resolves a non-empty ``contact.email``), and
    ``_cards_with_resolvable_email`` (the has_contact badge) filters ``email != ''`` too. If
    only ``is_(None)`` were checked, a higher-confidence ``''``-email row would win last-wins
    and resolve ``vendor_email=''`` → skip, while the badge promised contactable. Treating
    ``''`` like NULL here keeps the send path consistent with the badge.

    Called by: sightings_preview_inquiry, sightings_send_inquiry.
    """
    if not card_ids:
        return []
    return (
        db.query(VendorContact)
        .filter(VendorContact.vendor_card_id.in_(card_ids))
        .order_by(
            # Empty-or-NULL email rows first (lose last-wins) — '' is as unusable as NULL.
            or_(VendorContact.email.is_(None), VendorContact.email == "").desc(),
            VendorContact.is_verified.asc().nullsfirst(),  # verified rows last
            VendorContact.confidence.asc().nullsfirst(),  # higher confidence last
            VendorContact.id.asc(),  # deterministic tiebreak (newest row wins ties)
        )
        .all()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Offers tab (part-centric) — Convert-to-offer, Enter-offer, and mutations.
# Creation/mutation logic lives in app.services.offer_service (W3 "one
# offer_service"); these endpoints just adapt form input, gate essentials, and
# re-render #sightings-offers-panel.
# ─────────────────────────────────────────────────────────────────────────────


def _refresh_offers_panel(request: Request, requirement_id: int, db: Session) -> HTMLResponse:
    """Re-fetch the requirement (post-mutation) and render the offers panel, or 404."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    return _render_offers_panel(request, requirement, db)

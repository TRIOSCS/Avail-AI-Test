"""Shared state for the resell package — router, upload limits, status tuples, shared
helpers.

W4.8 split of the 2,830-line app/routers/resell.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...constants import (
    PG_INT4_MAX,
    PG_INT4_MIN,
    ExcessLineItemStatus,
    ExcessListStatus,
    ExcessOfferScope,
    ExcessOfferStatus,
)
from ...models import User
from ...models.excess import ExcessLineItem, ExcessList, ExcessOffer
from ...services import (
    excess_service,
)

router = APIRouter(tags=["resell"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls"}

# List statuses whose Sighting mirror is live OR completed (deal visible to non-owners).
# Drafts are excluded — only the owner may see a draft list; 404 for anyone else.
_POSTED_STATUSES = (
    ExcessListStatus.POSTED,
    ExcessListStatus.BIDDING,
    ExcessListStatus.AWARDED,
)
# Offer statuses that count as a live, unactioned offer (triage glance).
_UNACTIONED_OFFER_STATUSES = (ExcessOfferStatus.OPEN, ExcessOfferStatus.LATE)
# Offer statuses shown in the owner's Offers tab: the live bids (open/late), the winner
# (won), and the decided-competitor context (lost, rendered "Not selected"). A WITHDRAWN
# (or dead EXPIRED) offer is retracted and drops out of the tab entirely.
_VISIBLE_OFFER_STATUSES = (
    ExcessOfferStatus.OPEN,
    ExcessOfferStatus.LATE,
    ExcessOfferStatus.WON,
    ExcessOfferStatus.LOST,
)


# ── Helpers ──────────────────────────────────────────────────────────


def _file_extension(filename: str) -> str:
    """Return the lowercase extension (with dot), or '' if none."""
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()


# List statuses whose posting window CAN still be live (counting down). Only these — and
# only while the window's own ``close_at`` has not passed (:func:`_is_live`) — render the
# ``closes {countdown}`` chip; a resolved or deliberately-closed list must never
# show the red "Overdue" the shared time_text macro emits for a past deadline (finding #8;
# the old bid_out status collapsed into BIDDING in W3, so the "window over" fact now
# lives in ``close_at``).
_LIVE_STATUSES = (ExcessListStatus.POSTED, ExcessListStatus.BIDDING)


def _hours_until(close_at: datetime | None) -> float | None:
    """Hours until *close_at* (negative = overdue), or None when no close date.

    Drives the shared ``time_text`` urgency macro. Tolerates naive datetimes by
    stamping UTC — the close date is a coarse urgency signal.
    """
    if close_at is None:
        return None
    if close_at.tzinfo is None:
        close_at = close_at.replace(tzinfo=UTC)
    return (close_at - datetime.now(UTC)).total_seconds() / 3600.0


def _is_live(el: ExcessList) -> bool:
    """True while the list's posting window is live (posted/bidding AND not past close).

    Gates the countdown chip at the resell-template level (the shared ``time_text`` macro is
    NEVER edited — it's used by requisitions too). A resolved list — or a bidding list whose
    window was deliberately ended (``close_list`` stamps ``close_at``; the old ``bid_out``
    status) — is not live, so its chip renders a muted ``closed {date}`` instead of a red
    "Overdue".
    """
    return el.status in {s.value for s in _LIVE_STATUSES} and not excess_service._posting_window_closed(el)


def _close_at_display(close_at: datetime | None) -> str | None:
    """Coarse ``Mon DD`` label for a resolved list's muted "closed on" chip (or None).

    Returns a label ONLY for a window that has ACTUALLY closed — a ``close_at`` in the
    past. A missing deadline OR a still-future one yields None: a non-live list holding a
    future create-set deadline (a draft with an 'Offers close by' next week, or an awarded
    list whose deadline survived publish) has no "closed on" date and must not render
    "closed {future date}" (finding #2).

    Tolerates naive datetimes by stamping UTC (SQLite strips tzinfo), mirroring
    ``_hours_until``. Not an urgency signal — just when the window closed.
    """
    if close_at is None:
        return None
    if close_at.tzinfo is None:
        close_at = close_at.replace(tzinfo=UTC)
    if close_at > datetime.now(UTC):
        return None
    return close_at.strftime("%b %d")


def _offer_coverage(items: list[ExcessLineItem]) -> tuple[int, int]:
    """(lines with ≥1 offer, total lines) — the list's offer-coverage meter."""
    total = len(items)
    covered = sum(1 for it in items if (it.offer_count or 0) > 0)
    return covered, total


def _display_title(el: ExcessList, *, can_see_customer: bool) -> str:
    """The list title as shown to *this* viewer.

    Customer-identity hiding is view discipline: the owner sees the real free-text
    title, but a non-owner (the anonymized "Open to Me" lens, the non-owner detail,
    the submit-offer modal) gets a neutral, id-derived label instead. Traders name
    lists after the customer ("Acme Corp — surplus FPGAs"), so the raw title is the
    one field the ``customer_name`` anonymization doesn't sanitize — gate it the
    same way (same predicate that nulls the seller name in ``_list_cards``).
    """
    if can_see_customer:
        return el.title
    return f"Excess listing #{el.id}"


def _get_list_for_user(db: Session, list_id: int, user: User) -> tuple[ExcessList, bool]:
    """Fetch a list and decide whether *user* may see the seller's identity.

    The owner always sees the real customer; non-owners may only see the list when it is
    in a posted status (posted/bidding/awarded) — drafts are private to the owner (404,
    not 403, to avoid revealing the list's existence).
    """
    el = excess_service.get_excess_list(db, list_id)
    is_owner = el.owner_id == user.id
    if not is_owner and el.status not in {s.value for s in _POSTED_STATUSES}:
        # A non-owner may still reach a now-unposted list (closed/expired) IF they hold an
        # offer on it — so a broker can view/withdraw their own bid after the posting window
        # closes (finding #13). Drafts never carry offers, so this never reveals one. Only
        # runs on the rare non-posted, non-owner read (posted statuses short-circuit above).
        has_own_offer = db.scalar(
            select(ExcessOffer.id)
            .where(ExcessOffer.excess_list_id == el.id, ExcessOffer.submitted_by == user.id)
            .limit(1)
        )
        if has_own_offer is None:
            raise HTTPException(404, "List not found")
    return el, is_owner


def _require_owner(el: ExcessList, user: User) -> None:
    """Raise 403 if *user* is not the list owner.

    Used by mutation endpoints that only the owner may call (add-line, import-preview,
    import-confirm). Mirrors the guard in resell_publish.
    """
    if el.owner_id != user.id:
        raise HTTPException(403, "Only the list owner can edit it")


def _detail_context(
    request: Request, db: Session, el: ExcessList, user: User, *, items: list[ExcessLineItem] | None = None
) -> dict:
    """Build the shared detail context: chips + adaptive-shape flags.

    The adaptive rule (spec §"Flexible detail"): ``shape`` is ``single`` for a
    one-line deal (one card, no table chrome), ``table`` otherwise; ``take_all``
    offers render as a pinned banner above the lines regardless of shape.

    ``items`` may be passed in preloaded so a combined render (``_award_response_context``)
    runs the line-items SELECT once instead of once here AND once in ``_offers_context``.
    """
    if items is None:
        items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    can_see_customer = el.owner_id == user.id
    # The chip / tab badge must count exactly what the Offers tab renders (finding #18):
    # only _VISIBLE_OFFER_STATUSES — a withdrawn offer drops out of the tab, so it must
    # drop out of the count too (mirrors take_all_count's status filter below).
    offer_count = (
        db.query(func.count(ExcessOffer.id))
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.status.in_([s.value for s in _VISIBLE_OFFER_STATUSES]),
        )
        .scalar()
        or 0
    )
    take_all_count = (
        db.query(func.count(ExcessOffer.id))
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.scope == ExcessOfferScope.TAKE_ALL,
            ExcessOffer.status.in_([s.value for s in _UNACTIONED_OFFER_STATUSES]),
        )
        .scalar()
        or 0
    )
    return {
        "request": request,
        "user": user,
        "list": el,
        "display_title": _display_title(el, can_see_customer=can_see_customer),
        "line_items": items,
        "line_count": len(items),
        "awarded_line_count": sum(1 for it in items if it.status == ExcessLineItemStatus.AWARDED),
        "offer_count": offer_count,
        "take_all_count": take_all_count,
        "can_see_customer": can_see_customer,
        "can_post": excess_service.can_post(user),
        # PARKED (spec §5.3, W2.3 — trader offer lane): always False so the
        # Submit-offer button never renders (its routes are unregistered). The
        # original predicate — can_offer(user) AND non-owner AND posted status
        # (finding #50) — returns with the lane when a second trader user exists.
        "can_offer": False,
        "shape": "single" if len(items) == 1 else "table",
        "hours_until": _hours_until(getattr(el, "close_at", None)),
        # Chip gate (finding #8): countdown only while live; a resolved list shows a muted
        # "closed {date}" (never a red "Overdue").
        "is_live": _is_live(el),
        "close_at_display": _close_at_display(getattr(el, "close_at", None)),
        "is_posted": el.status in {s.value for s in _POSTED_STATUSES},
    }


def _toast(resp: Response, message: str) -> Response:
    """Attach the ``showToast`` HX-Trigger so an award/unaward confirms even though the
    triggering button was swapped out of the DOM (same pattern as
    sightings._with_toast)."""
    resp.headers["HX-Trigger"] = json.dumps({"showToast": {"message": message, "type": "success"}})
    return resp


# ── CSV export helpers ───────────────────────────────────────────────


def _fmt_dt(dt: datetime | None) -> str:
    """Minute-precision timestamp for a CSV cell (empty string when missing)."""
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


# ── tiny parse helpers (forms send strings) ──────────────────────────


def _to_decimal(value: str | None) -> Decimal | None:
    """Parse an optional money string → Decimal, or None when blank/invalid."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip().lstrip("$").replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _to_int(value: str | None) -> int | None:
    """Parse an optional integer string → int, or None when blank/invalid.

    Returns None for anything that cannot land in a Postgres INT4 column — an
    unparseable value, a non-finite float (``"inf"`` / ``"1e999"`` parse to ``inf`` and
    raise OverflowError inside ``int(float(...))``), or a value outside the signed 32-bit
    range — so a fat-fingered quantity/id trips the caller's ``is None`` guard (HTTP 400)
    instead of overflowing the column as an unhandled 500 at flush time.
    """
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(float(str(value).strip().replace(",", "")))
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed if PG_INT4_MIN <= parsed <= PG_INT4_MAX else None

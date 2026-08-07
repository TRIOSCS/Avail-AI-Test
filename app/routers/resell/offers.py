"""Offers tab — inbound offers, award/unaward/withdraw/assign, bids upload, exports.

W4.8 split of the 2,830-line app/routers/resell.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import json

from fastapi import Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, contains_eager, joinedload

from ...constants import (
    AccessKey,
    ExcessLineItemStatus,
    ExcessOfferScope,
    ExcessOfferStatus,
)
from ...database import get_db
from ...dependencies import require_access
from ...file_utils import ParseError, parse_tabular_file
from ...models import Company, User
from ...models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine
from ...services import (
    excess_service,
)
from ...template_env import template_response
from ...utils.csv_export import stream_csv
from .common import (
    _POSTED_STATUSES,
    _VISIBLE_OFFER_STATUSES,
    ALLOWED_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    _detail_context,
    _display_title,
    _file_extension,
    _fmt_dt,
    _get_list_for_user,
    _to_decimal,
    _to_int,
    _toast,
    router,
)

# Offer statuses a withdraw may act on: an inbound bid still in play. A won offer must be
# unawarded first (withdrawing it would strand its awarded lines); a lost/withdrawn offer
# is already closed.
_WITHDRAWABLE_OFFER_STATUSES = (ExcessOfferStatus.OPEN, ExcessOfferStatus.LATE)


def _offers_context(
    request: Request, db: Session, el: ExcessList, user: User, *, items: list[ExcessLineItem] | None = None
) -> dict:
    """Build the Offers tab context — per-line offer stacks + pinned take-all banners.

    Offers are the owner's private view: a non-owner gets an empty stack (the template
    renders the "offers are private" state instead). ``take_all_blocked`` is True once any
    line is already awarded, which disables a whole-list take-all award (it would collide
    with the per-line winner). Caller must have already authorized access to *el*.

    ``items`` may be passed in preloaded so a combined render (``_award_response_context``)
    runs the line-items SELECT once instead of once here AND once in ``_detail_context``.
    """
    is_owner = el.owner_id == user.id
    if items is None:
        items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    take_all_blocked = any(it.status == ExcessLineItemStatus.AWARDED for it in items)
    base = {
        "request": request,
        "user": user,
        "list": el,
        "line_items": items,
        "shape": "single" if len(items) == 1 else "table",
        "take_all_blocked": take_all_blocked,
    }

    if not is_owner:
        # Non-owner: render the viewer's OWN offers ONLY (finding #13). Carries NO
        # competitor data — no other broker's bids, no best-price rollup, no coverage/counts
        # (Phase-3 anonymization discipline). Scoped hard to submitted_by == user.id in the
        # active-visible set; an open/late own-offer is Withdraw-able (the withdraw route
        # already authorizes the submitter).
        own_offers = (
            db.scalars(
                select(ExcessOffer)
                .where(
                    ExcessOffer.excess_list_id == el.id,
                    ExcessOffer.submitted_by == user.id,
                    ExcessOffer.status.in_([s.value for s in _VISIBLE_OFFER_STATUSES]),
                )
                .options(joinedload(ExcessOffer.lines))
                .order_by(ExcessOffer.created_at.desc())
            )
            .unique()
            .all()
        )
        return {
            **base,
            "own_offers": own_offers,
            "by_line": {it.id: [] for it in items},
            "unmatched": [],
            "take_all_offers": [],
            "can_see_customer": False,
            "is_owner": False,
        }

    # Eager-load the broker identity + matched line the template reads (the same joinedloads
    # as the CSV-export twin), so the owner render never N+1s per offer/line (finding 1).
    # Kept db.query() — legacy auto-uniques the ``lines`` collection joinedload.
    _offer_eager = (
        joinedload(ExcessOffer.offerer_company),
        joinedload(ExcessOffer.offerer_vendor_card),
        joinedload(ExcessOffer.lines).joinedload(ExcessOfferLine.excess_line_item),
    )
    take_all_offers = (
        db.query(ExcessOffer)
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.scope == ExcessOfferScope.TAKE_ALL,
            ExcessOffer.status.in_([s.value for s in _VISIBLE_OFFER_STATUSES]),
        )
        .options(*_offer_eager)
        .order_by(ExcessOffer.created_at.desc())
        .all()
    )

    # Group per-line offer lines under their matched line item, plus an unmatched
    # queue for rows that didn't cleanly resolve (never dropped — spec §Offer-collection).
    by_line: dict[int, list] = {it.id: [] for it in items}
    unmatched: list = []
    per_line_offers = (
        db.query(ExcessOffer)
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.scope == ExcessOfferScope.PER_LINE,
            ExcessOffer.status.in_([s.value for s in _VISIBLE_OFFER_STATUSES]),
        )
        .options(*_offer_eager)
        .all()
    )
    for offer in per_line_offers:
        for line in offer.lines:
            entry = {"offer": offer, "line": line}
            if line.excess_line_item_id and line.excess_line_item_id in by_line:
                by_line[line.excess_line_item_id].append(entry)
            else:
                unmatched.append(entry)

    return {
        **base,
        "by_line": by_line,
        "unmatched": unmatched,
        "take_all_offers": take_all_offers,
        "can_see_customer": True,
        "is_owner": True,
    }


def _award_response_context(request: Request, db: Session, el: ExcessList, user: User) -> dict:
    """Combined context for the award/unaward OOB response.

    ``_award_response.html`` swaps the Offers tab (its primary target) and, out-of-band,
    the Lines tab (awarded/withdrawn pills) and the header chips (the awarded-count chip +
    list-status badge) — so awarding never resets the Alpine ``tab`` state. Merging the
    two contexts is safe: the shared keys (request/user/list/line_items/shape) are equal.

    Both sub-contexts run the identical ExcessLineItem SELECT, so load the line items ONCE
    here and thread them into both — the combined render issues the line-items query once.
    """
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    return {
        **_detail_context(request, db, el, user, items=items),
        **_offers_context(request, db, el, user, items=items),
    }


@router.get("/v2/partials/resell/{list_id}/offers", response_class=HTMLResponse)
async def resell_offers(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Lazy Offers tab body — per-line offer stacks + pinned take-all banner."""
    el, _ = _get_list_for_user(db, list_id, user)
    return template_response("htmx/partials/resell/_offers.html", _offers_context(request, db, el, user))


@router.get("/v2/partials/resell/{list_id}/offers/export")
async def resell_offers_export(
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Stream the list's collected inbound offers as a CSV download (owner-only).

    Mirrors the Offers tab: the SAME owner-only gate (offers are the owner's private view —
    a non-owner 403s, matching resell_line_offer_compare) and the SAME visible-offer set
    (open/late/won/lost). One row per per-line offer line, plus one row per take-all offer
    (which carries no lines). Broker identity is always shown here — the endpoint is
    owner-only, so the customer-anonymization that governs the "Open to Me" lens never applies.
    """
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not is_owner:
        raise HTTPException(403, "Offers are only visible to the list owner")

    offers = (
        db.query(ExcessOffer)
        .filter(
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.status.in_([s.value for s in _VISIBLE_OFFER_STATUSES]),
        )
        .options(
            joinedload(ExcessOffer.offerer_company),
            joinedload(ExcessOffer.offerer_vendor_card),
            joinedload(ExcessOffer.lines).joinedload(ExcessOfferLine.excess_line_item),
        )
        .order_by(ExcessOffer.created_at.desc(), ExcessOffer.id.desc())
        .all()
    )

    header = [
        "Offer ID",
        "Broker",
        "Scope",
        "MPN",
        "Quantity",
        "Unit Price",
        "Condition",
        "Lead Time (Days)",
        "Terms",
        "Take-All Total",
        "Status",
        "Received",
    ]

    def _rows():
        for offer in offers:
            broker = _offer_broker_label(offer)
            received = _fmt_dt(offer.created_at)
            if offer.scope == ExcessOfferScope.TAKE_ALL or not offer.lines:
                # Take-all binds the whole list with no line rows — one summary row.
                yield [
                    offer.id,
                    broker,
                    offer.scope,
                    "",
                    "",
                    "",
                    "",
                    "",
                    offer.notes,
                    offer.take_all_total_price,
                    offer.status,
                    received,
                ]
                continue
            for line in offer.lines:
                item = line.excess_line_item
                yield [
                    offer.id,
                    broker,
                    offer.scope,
                    line.mpn_raw,
                    line.quantity,
                    line.unit_price,
                    item.condition if item else "",
                    line.lead_time_days,
                    line.terms_text,
                    "",
                    offer.status,
                    received,
                ]

    return stream_csv(f"resell_offers_list_{el.id}.csv", header, _rows())


@router.get("/v2/partials/resell/{list_id}/lines/{line_id}/offers", response_class=HTMLResponse)
async def resell_line_offer_compare(
    request: Request,
    list_id: int,
    line_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Per-line offer comparison table (best highlighted + price-spread bar).

    Owner-only: the comparison reveals all competing brokers' prices, so non-owners
    receive 403 (not 404) to make the permission boundary explicit.

    Cloned from the (since-deleted) quote-builder modal. NO auto-select — the trader
    eyeballs terms / lead before picking (spec §Offer-collection).
    """
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not is_owner:
        raise HTTPException(403, "Offer comparison is only visible to the list owner")
    item = db.get(ExcessLineItem, line_id)
    if not item or item.excess_list_id != el.id:
        raise HTTPException(404, f"Line item {line_id} not found in list {list_id}")

    # Query the target rows DIRECTLY (perf): the old walk over the list's ``offers``
    # relationship lazy-loaded every offer + every offer's lines (1 + N queries) just to
    # filter down to one line in Python. One query: offer-lines on THIS line whose parent
    # offer is per-line and visible, with the offer (+ the broker identity the template's
    # _broker_label reads) eagerly attached.
    offer_lines = (
        db.query(ExcessOfferLine)
        .join(ExcessOffer, ExcessOfferLine.offer_id == ExcessOffer.id)
        .filter(
            ExcessOfferLine.excess_line_item_id == line_id,
            ExcessOffer.excess_list_id == el.id,
            ExcessOffer.scope == ExcessOfferScope.PER_LINE,
            ExcessOffer.status.in_([s.value for s in _VISIBLE_OFFER_STATUSES]),
        )
        .options(
            contains_eager(ExcessOfferLine.offer).joinedload(ExcessOffer.offerer_company),
            contains_eager(ExcessOfferLine.offer).joinedload(ExcessOffer.offerer_vendor_card),
        )
        .all()
    )
    rows = [{"offer": line.offer, "line": line} for line in offer_lines]

    priced = [r["line"].unit_price for r in rows if r["line"].unit_price is not None]
    return template_response(
        "htmx/partials/resell/offer_compare.html",
        {
            "request": request,
            "list": el,
            "item": item,
            "rows": rows,
            "min_price": float(min(priced)) if priced else None,
            "max_price": float(max(priced)) if priced else None,
            "is_owner": el.owner_id == user.id,
        },
    )


@router.get("/v2/partials/resell/{list_id}/bids/upload-form", response_class=HTMLResponse)
async def resell_bids_upload_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Render the Upload Bids modal (owner-only) — a compiled multi-bidder sheet
    upload."""
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not is_owner:
        raise HTTPException(403, "Only the list owner can upload a compiled bid sheet")
    return template_response(
        "htmx/partials/resell/upload_bids_modal.html",
        {"request": request, "list_id": el.id},
    )


# PARKED (spec §5.3, W2.3 — trader offer lane): route registration removed (no
# existing flag covers the lane; same mechanism as the W1 job parks in
# app/jobs/resell_jobs.py). Re-add the decorator
# ``@router.get("/v2/partials/resell/{list_id}/offer-form", response_class=HTMLResponse)``
# when a second trader user exists.
async def resell_offer_form(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Render the submit-offer modal (per-line / take-all scope toggle).

    PARKED — unrouted.
    """
    # Load-and-authorize: non-owners 404 on a draft (existence not revealed).
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not excess_service.can_offer(user):
        raise HTTPException(403, "You do not have permission to submit offers")
    if is_owner:
        raise HTTPException(403, "You cannot offer on your own excess list")
    if el.status not in {s.value for s in _POSTED_STATUSES}:
        raise HTTPException(404, "List not found")
    # Only ever rendered to a non-owner (is_owner 403s above), so the header shows the
    # anonymized label — never the seller-named free-text title. ``companies`` backs the
    # optional buyer-attribution select (#17 UI half) — the same CRM company list the create
    # modal exposes, not competitor-offer data (no can_see_customer leak). (id, name)
    # tuples only — the dropdown never needs the full Company entity.
    companies = db.execute(select(Company.id, Company.name).order_by(Company.name)).all()
    return template_response(
        "htmx/partials/resell/offer_form.html",
        {
            "request": request,
            "list": el,
            "display_title": _display_title(el, can_see_customer=is_owner),
            "companies": companies,
        },
    )


@router.post("/api/resell/{list_id}/bids/upload-preview", response_class=HTMLResponse)
async def resell_bids_upload_preview(
    request: Request,
    list_id: int,
    file: UploadFile,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Parse an uploaded compiled bid sheet and render the multi-bidder preview grid.

    Owner-only, posted-lists-only (mirrors ``submit_offer``'s list-status gate — a draft
    has no finalized lines to bid on). Same extension/size/ParseError guards as
    ``resell_import_preview``.
    """
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not is_owner:
        raise HTTPException(403, "Only the list owner can upload a compiled bid sheet")
    if el.status not in {s.value for s in _POSTED_STATUSES}:
        # Owner-only from here (403 above) — a camouflage 404 would mislead the one user
        # who can see the draft; say what unblocks the upload instead.
        raise HTTPException(400, "Post the list before uploading bids — offers are only collected on a posted list")
    filename = file.filename or ""
    if _file_extension(filename) not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type '{_file_extension(filename)}'")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large")
    try:
        rows = parse_tabular_file(content, filename)
    except ParseError as exc:
        raise HTTPException(400, "We couldn't read this file — it may be corrupt or not a valid spreadsheet") from exc
    if not rows:
        raise HTTPException(400, "No data rows found")

    result = excess_service.preview_bid_upload(db, list_id, rows)
    return template_response(
        "htmx/partials/resell/bid_upload_preview.html",
        {
            "request": request,
            "list_id": list_id,
            "filename": filename,
            **result,
            "carry_rows_json": json.dumps(result["carry_rows"]),
        },
    )


@router.post("/api/resell/{list_id}/bids/upload-confirm", response_class=HTMLResponse)
async def resell_bids_upload_confirm(
    request: Request,
    list_id: int,
    rows_json: str = Form(...),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Confirm a previewed compiled bid-sheet upload: ingest into offers, re-render the
    Offers tab.

    The service RE-CLASSIFIES every row fresh (never trusts the client's carried
    classification — mirrors ``confirm_import``'s L3 discipline). Responds with the
    ``_award_response.html`` OOB compose (primary body = Offers tab, the confirm form's
    hx-target; out-of-band = Lines tab + header chips) — the ingest recomputes per-line
    rollups and can flip ``posted → bidding``, so an Offers-only swap would leave the
    owner's chips and Lines-tab badges stale. An HX-Trigger toast summarizes the counts.
    """
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not is_owner:
        raise HTTPException(403, "Only the list owner can upload a compiled bid sheet")
    try:
        rows = json.loads(rows_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid bid upload payload") from exc
    if not isinstance(rows, list):
        raise HTTPException(400, "Invalid bid upload payload")
    # Guard EVERY element is a dict before the service normalizes rows (mirrors
    # resell_assemble_bid) — a tampered payload like [1] or ["x"] otherwise raises
    # AttributeError in _normalize_bid_row as an unhandled 500 instead of this 400.
    if not all(isinstance(r, dict) for r in rows):
        raise HTTPException(400, "Invalid bid upload payload")

    result = excess_service.upload_bids(db, list_id=list_id, user=user, rows=rows)

    el = excess_service.get_excess_list(db, list_id)
    resp = template_response(
        "htmx/partials/resell/_award_response.html", _award_response_context(request, db, el, user)
    )
    message = (
        f"{result['offers_created']} bid(s) uploaded ({result['lines_created']} lines, "
        f"{result['unmatched']} unmatched, {result['rejected']} rejected)"
    )
    superseded = result.get("superseded", 0)
    if superseded > 0:
        message += f" — replaced {superseded} earlier upload(s)"
    return _toast(resp, message)


# PARKED (spec §5.3, W2.3 — trader offer lane): route registration removed (no
# existing flag covers the lane). Only the parked submit-offer modal posted here;
# the owner's bid doors (outreach log-bid, bids upload) use their own endpoints.
# Re-add the decorator ``@router.post("/api/resell/{list_id}/offers",
# response_class=HTMLResponse)`` when a second trader user exists.
async def resell_submit_offer(
    request: Request,
    list_id: int,
    scope: str = Form(...),
    notes: str = Form(""),
    mpn_raw: str = Form(""),
    quantity: str = Form(""),
    unit_price: str = Form(""),
    lead_time_days: str = Form(""),
    terms_text: str = Form(""),
    take_all_total_price: str = Form(""),
    buyer_company_id: int | None = Form(None),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Submit an inbound offer (per_line single-entry or take_all) via the service.

    This slice handles the single-line quick-add path; the paste/upload funnel reuses
    the same preview grid (import_preview) and lands here per-row. The service enforces
    can_offer + the self-offer guard.
    """
    # Load-and-authorize: non-owners 404 on a draft (existence not revealed), and offers
    # are only accepted on a posted/published list — never on an unpublished draft.
    el, _ = _get_list_for_user(db, list_id, user)
    if el.status not in {s.value for s in _POSTED_STATUSES}:
        raise HTTPException(404, "List not found")

    scope = ExcessOfferScope(scope).value if scope in (s.value for s in ExcessOfferScope) else ExcessOfferScope.PER_LINE

    lines = None
    if scope == ExcessOfferScope.PER_LINE:
        qty = _to_int(quantity)
        # L2: reject a non-positive quantity here (400) — otherwise it reaches the
        # ExcessOfferLine @validates("quantity") ValueError as an unhandled 500.
        if not mpn_raw.strip() or qty is None or qty <= 0:
            raise HTTPException(400, "Per-line offer needs a part number and a positive quantity")
        lines = [
            {
                "mpn_raw": mpn_raw.strip(),
                "quantity": qty,
                "unit_price": _to_decimal(unit_price),
                "lead_time_days": _to_int(lead_time_days),
                "terms_text": terms_text or None,
            }
        ]

    excess_service.submit_offer(
        db,
        list_id=list_id,
        user=user,
        scope=scope,
        notes=notes or None,
        lines=lines,
        take_all_total_price=_to_decimal(take_all_total_price) if scope == ExcessOfferScope.TAKE_ALL else None,
        buyer_company_id=buyer_company_id,
    )

    el = excess_service.get_excess_list(db, list_id)
    return await resell_offers(request, list_id=el.id, user=user, db=db)


@router.post("/api/resell/{list_id}/offers/{offer_id}/award", response_class=HTMLResponse)
async def resell_award_offer(
    request: Request,
    list_id: int,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Award an inbound offer (owner-only): flip it to ``won``, mark its lines sold,
    recompute the winning buyer's scorecard, and re-render the Offers tab + OOB
    lines/chips.

    The service owns the transaction (buyer-score hook, mirror-retire, list-status
    derivation) and enforces owner-gating, 404 on a missing offer, and 409 when a line is
    already awarded to a different offer. The response is an OOB compose so awarding from a
    tab never resets the Alpine ``tab`` state; the toast fires via HX-Trigger.

    Finding #32: {list_id} is otherwise unused — a caller could POST an offer id that
    belongs to a DIFFERENT list under this URL's list_id (a stale tab, replayed request,
    or a crafted URL). 404 up front when the offer's real list disagrees, mirroring
    ``resell_withdraw_offer``'s existing guard — existence not revealed across lists.
    """
    offer = db.get(ExcessOffer, offer_id)
    if offer is None or offer.excess_list_id != list_id:
        raise HTTPException(404, f"Offer {offer_id} not found on list {list_id}")
    offer = excess_service.award_offer(db, offer_id, user)
    el = excess_service.get_excess_list(db, offer.excess_list_id)
    resp = template_response(
        "htmx/partials/resell/_award_response.html", _award_response_context(request, db, el, user)
    )
    return _toast(resp, "Offer awarded")


@router.post("/api/resell/{list_id}/offers/{offer_id}/unaward", response_class=HTMLResponse)
async def resell_unaward_offer(
    request: Request,
    list_id: int,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Reverse an award (owner-only): flip the offer back to ``open``, return its lines
    to the pool, and re-render the Offers tab + OOB lines/chips.

    The explicit inverse of award — never a silent auto-swap to a different winner. The
    service enforces owner-gating, 404 on a missing offer, and 409 when the offer is not
    awarded (nothing to reverse).

    Finding #32: 404 up front when the offer's real list disagrees with this URL's
    list_id, mirroring ``resell_withdraw_offer`` / ``resell_award_offer``.
    """
    offer = db.get(ExcessOffer, offer_id)
    if offer is None or offer.excess_list_id != list_id:
        raise HTTPException(404, f"Offer {offer_id} not found on list {list_id}")
    offer = excess_service.unaward_offer(db, offer_id, user)
    el = excess_service.get_excess_list(db, offer.excess_list_id)
    resp = template_response(
        "htmx/partials/resell/_award_response.html", _award_response_context(request, db, el, user)
    )
    return _toast(resp, "Award reversed")


@router.post("/api/resell/{list_id}/offers/{offer_id}/withdraw", response_class=HTMLResponse)
async def resell_withdraw_offer(
    request: Request,
    list_id: int,
    offer_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Withdraw an inbound offer, then re-render the Offers tab + OOB lines/chips.

    Authorized for the offer's SUBMITTER (a buyer retracting their own bid) OR the list
    OWNER (clearing a stale / erroneous offer). Only an open/late offer may be withdrawn —
    a won offer is 409 (unaward it first), and a lost/withdrawn one is already closed. The
    service (``withdraw_offer``) flips the status to ``withdrawn`` and recomputes every
    touched line's rollup; the withdrawn offer then drops out of the Offers tab.
    """
    offer = db.get(ExcessOffer, offer_id)
    if offer is None or offer.excess_list_id != list_id:
        raise HTTPException(404, f"Offer {offer_id} not found on list {list_id}")
    el = excess_service.get_excess_list(db, list_id)
    if user.id != offer.submitted_by and user.id != el.owner_id:
        raise HTTPException(403, "You can only withdraw your own offer")
    if offer.status not in {s.value for s in _WITHDRAWABLE_OFFER_STATUSES}:
        raise HTTPException(409, "Only an open offer can be withdrawn — unaward a won offer first")

    excess_service.withdraw_offer(db, offer_id)
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response(
        "htmx/partials/resell/_award_response.html", _award_response_context(request, db, el, user)
    )
    return _toast(resp, "Offer withdrawn")


@router.post("/api/resell/{list_id}/offer-lines/{offer_line_id}/assign", response_class=HTMLResponse)
async def resell_assign_offer_line(
    request: Request,
    list_id: int,
    offer_line_id: int,
    target_line_item_id: int = Form(...),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Assign an unmatched offer line to a posted line (owner-only), then re-render the
    Offers tab + OOB lines/chips.

    Manual resolution of the unmatched queue (finding #15): the salvaged line becomes a
    matched, awardable bid. The service owns the guards (404 list/offer-line/target, 403
    non-owner) and the rollup recompute; the response is the same OOB compose the award
    action uses so the Alpine tab state never resets.
    """
    excess_service.assign_offer_line(db, list_id, offer_line_id, target_line_item_id, user)
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response(
        "htmx/partials/resell/_award_response.html", _award_response_context(request, db, el, user)
    )
    return _toast(resp, "Offer assigned to line")


def _offer_broker_label(offer: ExcessOffer) -> str:
    """The competing broker's name for the owner-only offers export.

    Mirrors the ``_broker_label`` macro's owner branch (company → vendor card → id
    fallback). Only ever called from the owner-gated export, so it never anonymizes.
    """
    # Typed locals: both relationship chains are legacy untyped reads.
    label: str
    if offer.offerer_company:
        label = offer.offerer_company.name
        return label
    if offer.offerer_vendor_card:
        label = offer.offerer_vendor_card.display_name
        return label
    return f"Broker #{offer.id}"

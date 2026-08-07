"""Build-bid tab — assemble customer bid, PDF/CSV, send/accept/reject, bid sheet export.

W4.8 split of the 2,830-line app/routers/resell.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import json
import re

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from ...constants import (
    AccessKey,
    ExcessLineItemStatus,
)
from ...database import get_db
from ...dependencies import require_access, require_fresh_token
from ...models import User
from ...models.excess import CustomerBid, ExcessLineItem, ExcessList
from ...services import (
    bid_back_service,
    excess_service,
)
from ...template_env import template_response
from ...utils.csv_export import stream_csv
from .common import (
    _get_list_for_user,
    _require_owner,
    _to_decimal,
    _to_int,
    _toast,
    router,
)


@router.get("/v2/partials/resell/{list_id}/bid-sheet")
async def resell_bid_sheet_export(
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Stream a BLANK bid sheet CSV (owner-only) to compile multi-bidder responses.

    Same owner-only gate as ``resell_offers_export``. One row per ACTIVE line item
    (available/bidding — a withdrawn/awarded line has nothing left to bid on), plus blank
    bidder-fill columns (Bidder / Offer Qty / Unit Price / Lead Time (Days) / Notes) so
    several bidders' filled-in copies of this sheet can be concatenated into one compiled
    sheet and re-uploaded via ``/bids/upload-preview``. Line ID lets the upload path
    exact-match a row back to its posted line even if a bidder edited the part-number text.
    """
    el, is_owner = _get_list_for_user(db, list_id, user)
    if not is_owner:
        raise HTTPException(403, "The bid sheet is only available to the list owner")

    items = (
        db.query(ExcessLineItem)
        .filter(
            ExcessLineItem.excess_list_id == el.id,
            ExcessLineItem.status == ExcessLineItemStatus.AVAILABLE,
        )
        .order_by(ExcessLineItem.id)
        .all()
    )

    header = [
        "Line ID",
        "Part Number",
        "Manufacturer",
        "Description",
        "Qty Available",
        "Condition",
        "Date Code",
        "Bidder",
        "Offer Qty",
        "Unit Price",
        "Lead Time (Days)",
        "Notes",
    ]

    def _rows():
        for item in items:
            yield [
                item.id,
                item.part_number,
                item.manufacturer,
                item.description,
                item.quantity,
                item.condition,
                item.date_code,
                "",
                "",
                "",
                "",
                "",
            ]

    return stream_csv(f"resell_bid_sheet_list_{el.id}.csv", header, _rows())


# ── Build Bid tab (owner-only bid-back assembly) ─────────────────────


def _latest_bid(db: Session, list_id: int) -> CustomerBid | None:
    """The most recent CustomerBid for a list (the one the Build-Bid tab shows)."""
    return db.query(CustomerBid).filter(CustomerBid.excess_list_id == list_id).order_by(CustomerBid.id.desc()).first()


def _build_bid_context(request: Request, db: Session, el: ExcessList, user: User) -> dict:
    """Context for the Build-Bid tab: each line + its best-offer planning reference,
    plus the most recent assembled bid (its clean export summary) if one exists.

    Owner-only — the caller must gate access (the tab reveals planning prices). Each line
    surfaces ``best_offer_unit_price`` as the pre-fill reference for the editable "our
    offer" input; the summary renders the clean ``bid_back_export_context`` so the owner
    sees exactly what the customer doc will carry (no broker names).
    """
    items = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    bid = _latest_bid(db, el.id)
    summary = bid_back_service.bid_back_export_context(bid) if bid else None
    # Resolve the seller's send contact so the Send button can show WHERE the bid goes
    # (or disable + warn when no email is on file — never silently email nobody).
    recipient_name, recipient_email = bid_back_service.resolve_seller_contact(db, el)
    return {
        "request": request,
        "user": user,
        "list": el,
        "line_items": items,
        "line_count": len(items),
        "bid": bid,
        "summary": summary,
        "recipient_name": recipient_name,
        "recipient_email": recipient_email,
    }


@router.get("/v2/partials/resell/{list_id}/build-bid", response_class=HTMLResponse)
async def resell_build_bid(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Lazy Build-Bid tab body — owner-only bid-back builder.

    Reveals each line's best-offer planning price + an editable "our offer" input, an
    "Assemble bid" action, and (once assembled) the clean bid summary + a Download-PDF
    link. Non-owners get 403 on a posted list; a foreign private DRAFT 404-masks first
    (finding #48 — existence not revealed).
    """
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    return template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))


@router.post("/api/resell/{list_id}/bid", response_class=HTMLResponse)
async def resell_assemble_bid(
    request: Request,
    list_id: int,
    selections_json: str = Form(...),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Assemble a bid-back from the selected lines (owner-only), then re-render the tab.

    ``selections_json`` is a JSON array of ``{excess_line_item_id, customer_unit_price?}``
    (price blank → seeded from best_offer_unit_price). The service enforces owner-only +
    foreign-line rejection; this layer only parses the form and delegates. A foreign
    private DRAFT 404-masks before the owner 403 (finding #48).
    """
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    try:
        raw = json.loads(selections_json)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid bid payload") from exc
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "Select at least one line to assemble a bid")
    # Silent-failure c: guard EVERY element is a dict before the s.get(...) comprehension —
    # a payload like [1, 2] or ["x"] otherwise raises AttributeError as an unhandled 500.
    if not all(isinstance(s, dict) for s in raw):
        raise HTTPException(400, "Invalid bid payload")

    selections = [
        {
            "excess_line_item_id": _to_int(str(s.get("excess_line_item_id"))),
            "customer_unit_price": _to_decimal(str(s.get("customer_unit_price")))
            if s.get("customer_unit_price") not in (None, "")
            else None,
        }
        for s in raw
    ]
    # A missing/garbage/out-of-range excess_line_item_id coerces to None (_to_int) — 400
    # here, consistent with the adjacent payload-shape guards above, rather than letting
    # it reach build_bid_back's foreign-line guard as a confusing 404 "Line item None is
    # not part of list N" (finding #56, THEME E).
    if any(sel["excess_line_item_id"] is None for sel in selections):
        raise HTTPException(400, "Invalid bid payload")
    bid_back_service.build_bid_back(db, list_id=list_id, owner=user, selections=selections)
    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))


@router.get("/api/resell/{list_id}/bid/{bid_id}/pdf")
async def resell_bid_pdf(
    list_id: int,
    bid_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Download the clean bid-back PDF (owner-only).

    The bid must belong to *list_id* and the requester must own the list. The PDF
    renders only the whitelisted bid_back_export_context — no broker / trader / seller
    identity. A foreign private DRAFT 404-masks before the owner 403 (finding #48).
    """
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    bid = db.get(CustomerBid, bid_id)
    if not bid or bid.excess_list_id != list_id:
        raise HTTPException(404, f"Bid {bid_id} not found on list {list_id}")

    import asyncio

    from ...services.document_service import generate_bid_report_pdf

    loop = asyncio.get_running_loop()
    try:
        pdf_bytes = await loop.run_in_executor(None, generate_bid_report_pdf, bid.id, db)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=bid-{bid_id}.pdf"},
    )


@router.get("/api/resell/{list_id}/bid/{bid_id}/csv")
async def resell_bid_csv(
    list_id: int,
    bid_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Download the clean bid-back as CSV (owner-only) — the spreadsheet twin of the
    PDF.

    Built ONLY from :func:`bid_back_service.bid_back_export_context` (the identity-clean
    whitelist) — never the inbound offer/rollup/vendor fields — so the download carries no
    broker / trader / seller identity, same guarantee as the PDF. A trailing "Total" row
    carries the subtotal. Money cells are FORMATTED (unit 4dp, extended/total 2dp —
    matching the PDF's ``{:,.4f}``/``{:,.2f}``, sans thousands separators so spreadsheets
    parse them as numbers) — never raw float reprs (a 0.07 × 3 line must read "0.21",
    not "0.21000000000000002").
    """
    _excess_list, bid = bid_back_service.guard_bid_for_owner(db, list_id=list_id, bid_id=bid_id, owner=user)
    summary = bid_back_service.bid_back_export_context(bid)

    header = ["Part Number", "Manufacturer", "Condition", "Quantity", "Unit Price", "Extended Price"]

    def _money(value: float | None, places: int) -> str:
        return f"{value:.{places}f}" if value is not None else ""

    def _rows():
        for li in summary["line_items"]:
            yield [
                li["part_number"],
                li["manufacturer"],
                li["condition"],
                li["quantity"],
                _money(li["unit_price"], 4),
                _money(li["extended_price"], 2),
            ]
        yield ["Total", "", "", "", "", _money(summary["subtotal"], 2)]

    safe_number = re.sub(r"[^A-Za-z0-9_-]", "_", summary["bid_number"])
    return stream_csv(f"{safe_number}.csv", header, _rows())


@router.post("/api/resell/{list_id}/bid/{bid_id}/send", response_class=HTMLResponse)
async def resell_send_bid(
    request: Request,
    list_id: int,
    bid_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
    token: str = Depends(require_fresh_token),
):
    """Email the clean bid-back PDF to the customer (owner-only): flip ``draft→sent``.

    Delegates to :func:`bid_back_service.send_bid_back`, which resolves the seller
    contact, renders the whitelisted PDF, sends via the RFQ engine (no requisition), and
    stamps ``sent_at`` only on a confirmed send. Re-renders the Build-Bid tab with a
    toast. A missing contact email surfaces as a 422 the toast reports.
    """
    await bid_back_service.send_bid_back(db, list_id=list_id, bid_id=bid_id, owner=user, token=token)
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))
    return _toast(resp, "Bid sent to the customer")


@router.post("/api/resell/{list_id}/bid/{bid_id}/accept", response_class=HTMLResponse)
async def resell_accept_bid(
    request: Request,
    list_id: int,
    bid_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Record the customer's ACCEPTANCE of a sent bid (owner-only): ``sent→accepted``.

    Owner logs the seller's answer (the seller is not a User). Re-renders the Build-Bid
    tab with a toast. The service 409s if the bid is not ``sent`` (can't accept a draft).
    """
    bid_back_service.record_bid_response(db, list_id=list_id, bid_id=bid_id, owner=user, accepted=True)
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))
    return _toast(resp, "Bid marked accepted")


@router.post("/api/resell/{list_id}/bid/{bid_id}/reject", response_class=HTMLResponse)
async def resell_reject_bid(
    request: Request,
    list_id: int,
    bid_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Record the customer's REJECTION of a sent bid (owner-only): ``sent→rejected``.

    Owner logs the seller's answer. Re-renders the Build-Bid tab with a toast. The service
    409s if the bid is not ``sent``.
    """
    bid_back_service.record_bid_response(db, list_id=list_id, bid_id=bid_id, owner=user, accepted=False)
    el = excess_service.get_excess_list(db, list_id)
    resp = template_response("htmx/partials/resell/_build_bid.html", _build_bid_context(request, db, el, user))
    return _toast(resp, "Bid marked rejected")

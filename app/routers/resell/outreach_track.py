"""Outreach tracker — tracker table, replies, convert-to-offer, log response/bid,
export.

W4.8 split of the 2,830-line app/routers/resell.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload

from ...constants import (
    AccessKey,
    ExcessOutreachChannel,
    ExcessOutreachStatus,
)
from ...database import get_db
from ...dependencies import require_access
from ...models import User, VendorResponse
from ...models.excess import ExcessList, ExcessOutreach
from ...services import (
    buyer_affinity_service,
    excess_service,
    resell_outreach_service,
)
from ...template_env import template_response
from ...utils.csv_export import stream_csv
from .common import (
    _fmt_dt,
    _get_list_for_user,
    _require_owner,
    _to_decimal,
    _to_int,
    router,
)

# ── Outreach: offer-to-buyers panel + tracker + don't-forget strip ───
#
# The trader→buyer half of Resell (the inverse of sourcing's RFQ). Offering excess OUT
# is the list OWNER's action, so every endpoint here is owner-gated via _require_owner;
# the buyer panel + tracker reveal the buyer "who" and the team's outreach board, both
# the owner's private view. Logic lives in resell_outreach_service (send/log/reply) and
# buyer_affinity_service (rank/overlap/nudge); this layer only resolves request →
# context → template.

# Outreach statuses that count as a buyer ENGAGED at all (the tracker "responded" tally).
_RESPONDED_OUTREACH = (
    ExcessOutreachStatus.RESPONDED,
    ExcessOutreachStatus.BID,
    ExcessOutreachStatus.DECLINED,
)


def _outreach_tracker_context(request: Request, db: Session, el: ExcessList, user: User) -> dict:
    """Context for the unified Outreach tracker: rows (newest first) + the glance
    summary."""
    # B7: reclassify any of THIS list's rows stuck in ``sending`` past the staleness
    # threshold before rendering, so a row orphaned by a dead background send job becomes
    # actionable (Retry-able) the instant the tab is opened, instead of waiting on the
    # once-nightly sweep.
    resell_outreach_service.reclassify_stale_sending(db, excess_list_id=el.id)
    rows = (
        db.query(ExcessOutreach)
        .filter(ExcessOutreach.excess_list_id == el.id)
        # Eager-load the buyer / line / sender the template reads (the same joinedloads as
        # the CSV-export twin) so this render — which runs inside the 3s tracker poll — never
        # N+1s per row (finding 2). All many-to-one, so no .unique() is needed.
        .options(
            joinedload(ExcessOutreach.target_vendor_card),
            joinedload(ExcessOutreach.excess_line_item),
            joinedload(ExcessOutreach.submitted_by_user),
        )
        .order_by(ExcessOutreach.created_at.desc(), ExcessOutreach.id.desc())
        .all()
    )
    # Distinct-buyer counts so "offered N · M responded · K bid" reads per buyer, not per
    # (buyer × line) row — a 3-line per-line campaign is one buyer offered, not three. Only
    # genuinely-sent rows count as "offered": a ``sending`` / ``failed`` / ``interrupted``
    # row never reached the buyer, so it must not inflate the offered tally.
    offered = {
        r.target_vendor_card_id
        for r in rows
        if r.target_vendor_card_id is not None and r.status not in buyer_affinity_service._NOT_SENT_STATUSES
    }
    responded = {
        r.target_vendor_card_id for r in rows if r.target_vendor_card_id is not None and r.status in _RESPONDED_OUTREACH
    }
    bid = {
        r.target_vendor_card_id
        for r in rows
        if r.target_vendor_card_id is not None and r.status == ExcessOutreachStatus.BID
    }
    return {
        "request": request,
        "user": user,
        "list": el,
        "rows": rows,
        "summary": {"offered": len(offered), "responded": len(responded), "bid": len(bid)},
        # The shared not-sent set (as string values) so the template renders "—" for a
        # non-sent row's "When" instead of its meaningless created_at.
        "not_sent_statuses": [s.value for s in buyer_affinity_service._NOT_SENT_STATUSES],
        # Drives the tracker's self-poll: while any row is still ``sending`` (its
        # background send job has not finalized), the tab polls itself for the final state.
        "any_sending": any(r.status == ExcessOutreachStatus.SENDING for r in rows),
    }


def _conversation_replies(db: Session, conversation_id: str | None) -> list[VendorResponse]:
    """The buyer's inbound replies on ONE outreach conversation, newest-first.

    Narrow replacement for the old whole-list ``_replies_context`` map: the reply viewer
    renders a SINGLE conversation, so query only the ``VendorResponse`` rows on this
    ``graph_conversation_id`` (served by ``ix_vr_conversation``, migration 200) instead of
    building the map for every conversation on the list. ``VendorResponse`` is the buyer's
    inbound email (one per received message, written by the inbox poll, carrying the reply
    body), joined back to the outreach on the shared ``graph_conversation_id`` the send path
    stamped. Returns [] for a None / unmatched id (an unstamped row has no thread to show).
    """
    if not conversation_id:
        return []
    return (
        db.query(VendorResponse)
        .filter(VendorResponse.graph_conversation_id == conversation_id)
        .order_by(VendorResponse.received_at.desc())
        .all()
    )


@router.get("/v2/partials/resell/{list_id}/outreach", response_class=HTMLResponse)
async def resell_outreach_tracker(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Lazy Outreach tab body — the unified tracker (owner-only).

    One row per buyer×line touch reading buyer · when · by-whom · channel · status,
    above the "offered N · M responded · K bid" glance. Owner's private board (403
    otherwise).
    """
    # 404-mask a foreign private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))


@router.get("/v2/partials/resell/{list_id}/outreach/export")
async def resell_outreach_export(
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Stream the list's outreach tracker as a CSV download (owner-only).

    Reuses the tracker's row query + the SAME owner gate (_require_owner) as the tracker
    tab: one row per buyer x line touch — buyer · line · channel · by · status · sent ·
    last activity — newest first (identical order to the tab).
    """
    # 404-mask a foreign private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    rows = (
        db.query(ExcessOutreach)
        .filter(ExcessOutreach.excess_list_id == el.id)
        .options(
            joinedload(ExcessOutreach.target_vendor_card),
            joinedload(ExcessOutreach.excess_line_item),
            joinedload(ExcessOutreach.submitted_by_user),
        )
        .order_by(ExcessOutreach.created_at.desc(), ExcessOutreach.id.desc())
        .all()
    )

    header = ["Buyer", "Line", "Channel", "Sent By", "Status", "Sent At", "Last Activity", "Note"]
    not_sent = buyer_affinity_service._NOT_SENT_STATUSES

    def _rows():
        for r in rows:
            # Mirror the tracker's "When": a non-sent row (sending / failed / interrupted)
            # never reached the buyer, so its created_at is NOT a real send time — leave the
            # "Sent At" cell blank instead of misreporting the row-creation time as a send.
            sent_at = "" if r.status in not_sent else _fmt_dt(r.sent_at or r.created_at)
            yield [
                r.target_vendor_card.display_name if r.target_vendor_card else "Unknown buyer",
                r.excess_line_item.part_number if r.excess_line_item else "Whole list",
                r.channel,
                r.submitted_by_user.name if r.submitted_by_user else "",
                r.status,
                sent_at,
                _fmt_dt(r.updated_at),
                # Surface the persisted send-failure / degraded-reply-matching reason so an
                # exported failed/interrupted (or delivered-but-degraded) row is not silent.
                r.send_error or "",
            ]

    return stream_csv(f"resell_outreach_list_{el.id}.csv", header, _rows())


def _load_outreach_for_owner(
    db: Session, list_id: int, outreach_id: int, user: User
) -> tuple[ExcessList, ExcessOutreach]:
    """Owner-gated load of one outreach row on a list, requiring an email thread.

    404 (not 403) when the row is missing or belongs to another list — existence is not
    revealed. 404 when the row has no ``graph_conversation_id`` (a manual-log or degraded
    send has no thread to view or convert). Shared by the reply-viewer + convert-to-offer
    routes so both enforce the same guard.
    """
    # 404-mask a foreign private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    outreach = db.get(ExcessOutreach, outreach_id)
    if outreach is None or outreach.excess_list_id != el.id:
        raise HTTPException(404, "Outreach not found")
    if not outreach.graph_conversation_id:
        raise HTTPException(404, "No email thread on this outreach")
    return el, outreach


def _load_manual_outreach_for_owner(
    db: Session, list_id: int, outreach_id: int, user: User
) -> tuple[ExcessList, ExcessOutreach]:
    """Owner-gated load of a MANUAL (or degraded-email) outreach row (finding #12; B3).

    404 (not 403) when the row is missing or belongs to another list — existence is not
    revealed. 409 for an email row that HAS a thread (``graph_conversation_id`` set): an
    emailed touch with a thread has its outcome logged via the reply viewer / inbox
    matcher, not the manual-log path. A DEGRADED email row — status SENT/FAILED but no
    ``graph_conversation_id`` ever captured (the Graph-id lookup came back empty at send
    time) — has no thread to view and is otherwise a hard dead end (the reply viewer 404s
    it too, per ``_load_outreach_for_owner``): it is explicitly allowed through here so the
    manual log-response/log-bid path is its one remaining outcome-logging route. Shared by
    the manual log-response / log-bid routes.
    """
    # 404-mask a foreign private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    outreach = db.get(ExcessOutreach, outreach_id)
    if outreach is None or outreach.excess_list_id != el.id:
        raise HTTPException(404, "Outreach not found")
    if outreach.channel == ExcessOutreachChannel.EMAIL and outreach.graph_conversation_id:
        raise HTTPException(409, "Use the reply viewer to log an email outreach's outcome")
    return el, outreach


@router.get("/v2/partials/resell/{list_id}/outreach/{outreach_id}/reply", response_class=HTMLResponse)
async def resell_outreach_reply(
    request: Request,
    list_id: int,
    outreach_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Reply viewer for one buyer×list outreach (owner-only): the reply thread + a
    convert-to-offer quick-add.

    Loads the buyer's inbound emails on this outreach's conversation and renders them
    with a "Convert to offer" form, so the trader can turn a free-text reply into a
    tracked inbound ExcessOffer. Owner's private view (403 non-owner); 404 when the
    outreach has no email thread.
    """
    el, outreach = _load_outreach_for_owner(db, list_id, outreach_id, user)
    replies = _conversation_replies(db, outreach.graph_conversation_id)
    return template_response(
        "htmx/partials/resell/_reply_viewer.html",
        {
            "request": request,
            "user": user,
            "list": el,
            "outreach": outreach,
            "replies": replies,
        },
    )


@router.post("/api/resell/{list_id}/outreach/{outreach_id}/offer", response_class=HTMLResponse)
async def resell_outreach_convert_offer(
    request: Request,
    list_id: int,
    outreach_id: int,
    mpn_raw: str = Form(""),
    quantity: str = Form(""),
    unit_price: str = Form(""),
    lead_time_days: str = Form(""),
    terms_text: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Convert a buyer's reply into a tracked inbound offer (owner-only), then re-render
    the tracker.

    Human-reviewed offer extraction: the trader reads the reply and types the line. Reuses
    :func:`resell_outreach_service.record_response` (``has_offer=True``) so the offer is
    created via the SAME queued-never-dropped line matcher as an emailed bid and the
    outreach advances sent/responded → ``bid``. Owner-gated; 404 when there is no thread.
    """
    el, outreach = _load_outreach_for_owner(db, list_id, outreach_id, user)

    qty = _to_int(quantity)
    # L2: reject a non-positive quantity here (400) — mirrors resell_submit_offer / log-bid.
    # A negative qty is not None, so without the ``qty <= 0`` clause it flows into
    # _link_inbound_offer and trips the ExcessOfferLine @validates('quantity') ValueError as
    # an unhandled 500, while a 0 was silently promoted to 1 by the old ``or 1`` coercion.
    if not mpn_raw.strip() or qty is None or qty <= 0:
        raise HTTPException(400, "A converted offer needs a part number and a positive quantity")

    resell_outreach_service.record_response(
        db,
        conversation_id=outreach.graph_conversation_id,
        has_offer=True,
        offer_lines=[
            {
                "mpn_raw": mpn_raw.strip(),
                "quantity": qty,
                "unit_price": _to_decimal(unit_price),
                "lead_time_days": _to_int(lead_time_days),
                "terms_text": terms_text or None,
            }
        ],
        offer_notes=notes or None,
    )

    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))


@router.post("/api/resell/{list_id}/outreach/{outreach_id}/log-response", response_class=HTMLResponse)
async def resell_outreach_log_response(
    request: Request,
    list_id: int,
    outreach_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Log a manual-channel buyer RESPONSE (owner-only), then re-render the tracker.

    The manual counterpart to the inbox reply matcher for a phone/teams/marketplace touch
    that has no email thread: advances the row sent → responded (never regresses a terminal
    bid/declined). 409 for an email row (use the reply viewer).
    """
    _el, outreach = _load_manual_outreach_for_owner(db, list_id, outreach_id, user)
    resell_outreach_service.record_manual_response(db, outreach=outreach, has_offer=False)
    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))


@router.get("/v2/partials/resell/{list_id}/outreach/{outreach_id}/log-bid-form", response_class=HTMLResponse)
async def resell_outreach_log_bid_form(
    request: Request,
    list_id: int,
    outreach_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Render the Log-their-bid modal for a manual-channel row (owner-only).

    Reuses the reply viewer's convert-to-offer line form, pointed at the manual log-bid
    route (a manual touch has no reply thread, so the ``manual`` flag renders the honest
    "logged manually" note instead of an email thread).
    """
    el, outreach = _load_manual_outreach_for_owner(db, list_id, outreach_id, user)
    return template_response(
        "htmx/partials/resell/_reply_viewer.html",
        {
            "request": request,
            "user": user,
            "list": el,
            "outreach": outreach,
            "replies": [],
            "manual": True,
            "convert_url": f"/api/resell/{el.id}/outreach/{outreach.id}/log-bid",
        },
    )


@router.post("/api/resell/{list_id}/outreach/{outreach_id}/log-bid", response_class=HTMLResponse)
async def resell_outreach_log_bid(
    request: Request,
    list_id: int,
    outreach_id: int,
    mpn_raw: str = Form(""),
    quantity: str = Form(""),
    unit_price: str = Form(""),
    lead_time_days: str = Form(""),
    terms_text: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Log a manual-channel buyer BID (owner-only): create the inbound ExcessOffer +
    advance the row → bid, then re-render the tracker.

    Reuses ``record_manual_response(has_offer=True)`` → the SAME queued-never-dropped line
    matcher an emailed bid uses. 409 for an email row (use the reply viewer).
    """
    _el, outreach = _load_manual_outreach_for_owner(db, list_id, outreach_id, user)

    qty = _to_int(quantity)
    # L2: reject a non-positive quantity here (400) — mirrors resell_submit_offer. A
    # negative qty is not None, so without the ``qty <= 0`` clause it flows into
    # _link_inbound_offer and trips the ExcessOfferLine @validates('quantity') ValueError
    # as an unhandled 500, after the parent ExcessOffer row was already flushed.
    if not mpn_raw.strip() or qty is None or qty <= 0:
        raise HTTPException(400, "A logged bid needs a part number and a positive quantity")

    resell_outreach_service.record_manual_response(
        db,
        outreach=outreach,
        has_offer=True,
        offer_lines=[
            {
                "mpn_raw": mpn_raw.strip(),
                "quantity": qty,
                "unit_price": _to_decimal(unit_price),
                "lead_time_days": _to_int(lead_time_days),
                "terms_text": terms_text or None,
            }
        ],
        offer_notes=notes or None,
    )

    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))

"""Outreach send path — buyer suggestion panel, submit + retry outreach.

W4.8 split of the 2,830-line app/routers/resell.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import os
from datetime import UTC, datetime

from fastapi import BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...constants import (
    AccessKey,
    ExcessListStatus,
    ExcessOfferStatus,
    ExcessOutreachChannel,
    ExcessOutreachStatus,
)
from ...database import get_db
from ...dependencies import require_access, require_fresh_token
from ...models import User, VendorCard
from ...models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine, ExcessOutreach
from ...services import (
    buyer_affinity_service,
    excess_service,
    resell_outreach_service,
    task_service,
)
from ...template_env import template_response
from .common import _get_list_for_user, _require_owner, _to_int, router
from .outreach_track import _outreach_tracker_context


def _suggestion_rows(db: Session, el: ExcessList, owner: User, line_ids: list[int] | None) -> list[dict]:
    """Ranked, offerable buyer suggestions for the panel, each with its advisory overlap
    flag.

    Wraps ``buyer_affinity_service.rank_buyers_for`` (already bounded + reachable-only)
    and decorates each row with ``overlap_warning`` (advisory — never blocks). Scoped to
    the selected lines when given, else the whole list.
    """
    ranked = buyer_affinity_service.rank_buyers_for(
        db,
        excess_list_id=el.id if not line_ids else None,
        line_item_ids=line_ids or None,
    )
    # Batch the advisory overlap flag for every ranked buyer (M8: was one query per buyer).
    overlaps = buyer_affinity_service.overlap_warnings_for(
        db,
        excess_list_id=el.id,
        target_vendor_card_ids=[rb.vendor_card_id for rb in ranked],
        owner_id=owner.id,
    )
    return [{"buyer": rb, "overlap": overlaps.get(rb.vendor_card_id)} for rb in ranked]


def _no_contact_buyers(db: Session, el: ExcessList, suggested_ids: set[int]) -> list[dict]:
    """Buyers with offer history on this list's lines but NO resolvable contact email.

    rank_buyers_for filters unreachable buyers out (it only returns offerable ones), but
    a buyer the owner has bought-from before may have lost their contact email — the
    panel still lists them (manual-log only) with a clear "no contact on file" badge so
    they're never silently dropped (mirrors the RFQ modal's no-email treatment). Returns
    [{card, last_bid}] for the history buyers absent from the reachable suggestions.
    """
    line_ids = [li.id for li in db.query(ExcessLineItem.id).filter_by(excess_list_id=el.id).all()]
    # History buyers: won an offer on one of this list's lines (the strongest "we've
    # dealt with them" signal — the ones worth surfacing even when unreachable).
    history_ids = {
        cid
        for (cid,) in db.query(ExcessOffer.offerer_vendor_card_id)
        .join(ExcessOfferLine, ExcessOfferLine.offer_id == ExcessOffer.id)
        .filter(
            ExcessOffer.status == ExcessOfferStatus.WON,
            ExcessOffer.offerer_vendor_card_id.isnot(None),
            ExcessOfferLine.excess_line_item_id.in_(line_ids) if line_ids else False,
        )
        .distinct()
        .all()
    }
    missing = history_ids - suggested_ids
    if not missing:
        return []
    cards = db.query(VendorCard).filter(VendorCard.id.in_(list(missing))).all()
    return [{"card": c} for c in cards]


def _buyer_panel_context(
    request: Request,
    db: Session,
    el: ExcessList,
    owner: User,
    line_ids: list[int] | None,
    preselect_ids: list[int] | None = None,
) -> dict:
    """Context for the offer-to-buyers panel: ranked suggestions + no-contact buyers +
    scope.

    ``preselect_ids`` (buyer ``vendor_card_id``s) seed the panel's checked set so a "not
    yet offered" nudge chip lands with its buyer already selected (RS-8) — one click from
    action instead of re-finding the buyer in the ranked list.

    ``line_ids`` are URL-supplied and MUST be scoped to *el* (findings #35/#49): a foreign
    list's line ids would otherwise render that list's parts in the scope strip AND skew
    the ranking toward another deal's commodities. Any id not on this list is a 422 —
    mirroring the POST twin's ``_target_line_ids`` guard.
    """
    scope_lines = None
    if line_ids:
        scope_lines = (
            db.query(ExcessLineItem)
            .filter(ExcessLineItem.id.in_(line_ids), ExcessLineItem.excess_list_id == el.id)
            .all()
        )
        bad = sorted(set(line_ids) - {li.id for li in scope_lines})
        if bad:
            raise HTTPException(422, f"Line item(s) {bad} are not on list {el.id}")
        line_ids = [li.id for li in scope_lines]
    # PARKED (spec §5.3, W2.3 — buyer-intelligence display): ranked suggestions and
    # the no-contact history rows are parked until a second trader user exists —
    # the panel renders its existing "No ranked buyers yet — search to add one
    # below" empty state and the manual-add path keeps outreach working.
    # ``_suggestion_rows`` / ``_no_contact_buyers`` stay in place; restore
    # ``suggestions = _suggestion_rows(db, el, owner, line_ids)`` and
    # ``_no_contact_buyers(db, el, {row["buyer"].vendor_card_id for row in
    # suggestions})`` on comeback.
    suggestions: list[dict] = []
    # Line count for the neutral outreach subject prefill (#11) — the campaign's scope:
    # the selected lines, or the whole list. NEVER the title (which names the customer).
    line_count = (
        len(scope_lines)
        if scope_lines
        else (db.scalar(select(func.count(ExcessLineItem.id)).where(ExcessLineItem.excess_list_id == el.id)) or 0)
    )
    return {
        "request": request,
        "user": owner,
        "list": el,
        "suggestions": suggestions,
        "no_contact_buyers": [],
        "channels": [c.value for c in ExcessOutreachChannel],
        "line_ids": line_ids or [],
        "scope_lines": scope_lines,
        "line_count": line_count,
        "preselect_ids": preselect_ids or [],
    }


@router.get("/v2/partials/resell/{list_id}/offer-buyers-form", response_class=HTMLResponse)
async def resell_offer_buyers_form(
    request: Request,
    list_id: int,
    line_ids: str = Query(""),
    preselect_vendor_card_id: str = Query(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Render the offer-to-buyers panel (owner-only): ranked suggestions + manual add +
    scope + channel.

    ``line_ids`` (comma-separated) scopes the campaign to specific lines; omitted = the
    whole list. ``preselect_vendor_card_id`` seeds the checked set so a "not yet offered"
    nudge chip opens the panel with that buyer already selected (RS-8). The panel reveals
    the buyer "who" + scorecard facts, so it is the owner's private view (403 for a
    non-owner).
    """
    # 404-mask a foreign private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    parsed = [lid for lid in (_to_int(x) for x in line_ids.split(",")) if lid is not None] if line_ids else None
    preselect = _to_int(preselect_vendor_card_id)
    return template_response(
        "htmx/partials/resell/offer_buyers_modal.html",
        _buyer_panel_context(request, db, el, user, parsed, [preselect] if preselect is not None else None),
    )


# PARKED (spec §5.3, W2.3 — buyer-intelligence display): route registration removed
# (no existing flag covers the layer). This nudge strip + its auto My-Day follow-up
# task writes park together; detail.html's lazy embed was removed with it. Re-add the
# decorator ``@router.get("/v2/partials/resell/{list_id}/not-yet-strip",
# response_class=HTMLResponse)`` (and the detail.html embed) when a second trader
# user exists.
async def resell_not_yet_strip(
    request: Request,
    list_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """The "usually offered, not yet this round" nudge strip (owner-only).

    Wired into the EXISTING detail nudge surface AND, per CRM Phase 2, also persists
    each surfaced buyer as a durable My-Day follow-up task for the list owner so the
    nudge survives a page close. The in-page strip and the task share the same buyer
    set; task creation is idempotent per (list, buyer, owner) via the Task service, so
    reloading the strip never duplicates a buyer's task.
    """
    # 404-mask a foreign private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    buyers = buyer_affinity_service.not_yet_offered_strip(db, excess_list_id=el.id)
    # Persist the nudge as owner-assigned My-Day follow-up tasks (idempotent). Due today
    # so it lands under the Tasks page "Due soon" bucket. Batched: ONE existing-task IN
    # query + ONE commit for the whole strip (the per-buyer loop ran a SELECT + COMMIT per
    # buyer inside this GET render).
    task_service.auto_create_resell_followup_tasks(
        db,
        excess_list_id=el.id,
        owner_id=el.owner_id,
        buyers=[(b.vendor_card_id, b.display_name) for b in buyers],
        due_at=datetime.now(UTC),
    )
    return template_response(
        "htmx/partials/resell/_not_yet_strip.html",
        {"request": request, "user": user, "list": el, "buyers": buyers},
    )


@router.post("/api/resell/{list_id}/outreach", response_class=HTMLResponse)
async def resell_submit_outreach(
    request: Request,
    background_tasks: BackgroundTasks,
    list_id: int,
    vendor_card_ids: str = Form(""),
    company_ids: str = Form(""),
    scope: str = Form("whole_list"),
    channel: str = Form(ExcessOutreachChannel.EMAIL),
    line_ids: str = Form(""),
    notes: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
):
    """Submit an outreach campaign (owner-only), then re-render the tracker.

    Buyers arrive as ``vendor_card_ids`` (ranked picks) and/or ``company_ids`` (a
    manual-add company with no card yet — the service backfills a card). ``channel`` ==
    ``email`` writes the tracker rows in the transient ``sending`` state via
    :func:`resell_outreach_service.enqueue_outreach_email` and hands the actual send +
    per-buyer sent-message lookups to a background job
    (:func:`resell_outreach_service.run_outreach_email_send`) so a multi-buyer send never
    blocks the modal — the tracker re-renders at once showing ``sending`` and polls itself
    to the final status. Any other channel is a manual log via :func:`submit_outreach`.
    ``scope`` is ``per_line`` (scoped to ``line_ids``) or ``whole_list``. The service
    enforces the owner + can_post guards.

    Only the EMAIL channel actually sends via Graph, so the M365 token is acquired
    in-branch there (the quotes.py send precedent) instead of a
    route-level ``Depends(require_fresh_token)`` — logging a manual/phone outreach must
    work with no M365 token at all. A keys-off email submit gets an honest 409 naming
    the missing M365 connection, not a login-bounce 401 for a user who IS logged in.
    """
    # 404-mask a foreign private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    if el.status == ExcessListStatus.DRAFT:
        raise HTTPException(409, "List is not posted")
    if channel not in {c.value for c in ExcessOutreachChannel}:
        raise HTTPException(422, "Unknown channel")

    buyers: list[dict] = [{"vendor_card_id": cid} for cid in (_to_int(x) for x in vendor_card_ids.split(",")) if cid]
    buyers += [{"company_id": cid} for cid in (_to_int(x) for x in company_ids.split(",")) if cid]
    if not buyers:
        raise HTTPException(400, "Select at least one buyer to offer")

    parsed_lines = [lid for lid in (_to_int(x) for x in line_ids.split(",")) if lid is not None] if line_ids else None
    scope_value = scope if scope in ("per_line", "whole_list") else "whole_list"

    if channel == ExcessOutreachChannel.EMAIL:
        if not subject.strip() or not body.strip():
            raise HTTPException(400, "An email outreach needs a subject and a message")
        # In-branch token acquisition (see docstring). Skipped in TESTING — a direct call
        # (not Depends) would 401 in tests, and the service skips the real Graph send
        # there anyway; the 401→409 rewrite keeps the failure honest for a logged-in
        # user with no (or an expired) M365 connection.
        token = ""
        if os.environ.get("TESTING") != "1":
            try:
                token = await require_fresh_token(request, db)
            except HTTPException as exc:
                raise HTTPException(
                    409,
                    "Microsoft 365 isn't connected, so this email can't be sent — "
                    "reconnect M365, or log the outreach under a manual channel.",
                ) from exc
        # Phase 1 (fast, inline): write the rows as ``sending`` and return at once.
        _rows, plan = resell_outreach_service.enqueue_outreach_email(
            db,
            list_id=list_id,
            owner=user,
            buyers=buyers,
            scope=scope_value,
            subject=subject.strip(),
            body=body.strip(),
            line_item_ids=parsed_lines,
        )
        # Phase 2 (background): the multi-buyer send + per-buyer Graph sent-message
        # lookups run off the request path and advance each row to its final status.
        background_tasks.add_task(
            resell_outreach_service.run_outreach_email_send,
            list_id=list_id,
            owner_id=user.id,
            subject=subject.strip(),
            body=body.strip(),
            token=token,
            groups=plan,
        )
    else:
        resell_outreach_service.submit_outreach(
            db,
            list_id=list_id,
            owner=user,
            buyers=buyers,
            scope=scope_value,
            channel=channel,
            line_item_ids=parsed_lines,
            notes=notes or None,
        )

    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))


# Retry prefers the EXACT subject/body the campaign was sent with (persisted on the row
# since Phase 2: ``send_subject`` / ``send_body``) so the Sent-folder reconcile matches and
# a customized send re-sends verbatim. These are only the FALLBACK for a row missing that
# persisted text (legacy / cleared-subject rows). The subject ships EXTERNALLY to the buyer,
# so the fallback must stay anonymized — a part-count subject, NEVER ``el.title`` (which
# traders write as the customer name, the #11/#12 leak class the modal prefill + internal
# ActivityLog subject already neutralized). Kept in sync with offer_buyers_modal.html.
_RETRY_BODY = "We have the following excess available — let us know if you'd like to bid."


def _neutral_outreach_subject(line_count: int) -> str:
    """Neutral, part-count outreach subject — mirrors the compose-modal prefill.

    Used as the retry resend's fallback subject when a row has no persisted ``send_subject``.
    NEVER embeds ``el.title`` (the customer name), so the anonymized listing stays anonymized
    on the external send. Matches ``offer_buyers_modal.html``'s ``Excess available: N line(s)``.
    """
    return f"Excess available: {line_count} line" + ("s" if line_count != 1 else "")


# Outreach statuses a failed send can be retried FROM (a genuine send failure or an
# interrupted/orphaned send — never a live sending/sent/engaged row).
_RETRYABLE_OUTREACH = (ExcessOutreachStatus.FAILED, ExcessOutreachStatus.INTERRUPTED)


@router.post("/api/resell/{list_id}/outreach/{outreach_id}/retry", response_class=HTMLResponse)
async def resell_retry_outreach(
    request: Request,
    background_tasks: BackgroundTasks,
    list_id: int,
    outreach_id: int,
    user: User = Depends(require_access(AccessKey.RESELL)),
    db: Session = Depends(get_db),
    token: str = Depends(require_fresh_token),
):
    """Retry a failed / interrupted EMAIL outreach (owner-only), then re-render the
    tracker.

    Optimistically flips the row back to ``sending`` (so the tracker re-render shows it +
    polls) and hands the reconcile-first resend to a background job
    (:func:`resell_outreach_service.retry_outreach_send`), which re-checks the Sent folder
    BEFORE resending so an already-delivered row is reconciled to ``sent`` instead of
    double-sent. 404 when the row is missing / on another list; 409 when it is not in a
    retryable state or is not an email outreach.
    """
    # 404-mask a foreign private draft (finding #48) BEFORE the owner 403.
    el, _ = _get_list_for_user(db, list_id, user)
    _require_owner(el, user)
    row = db.get(ExcessOutreach, outreach_id)
    if row is None or row.excess_list_id != el.id:
        raise HTTPException(404, "Outreach not found")
    if row.channel != ExcessOutreachChannel.EMAIL:
        raise HTTPException(409, "Only an email outreach can be retried")
    # B7: a row stuck in ``sending`` past the staleness threshold (its background send job
    # died) becomes actionable HERE instead of waiting for the once-nightly sweep — without
    # this a genuinely-orphaned row hard-409s below for up to ~24h even though the code's
    # own threshold already knows it is orphaned.
    resell_outreach_service.reclassify_stale_sending(db, outreach_id=row.id)
    if row.status not in _RETRYABLE_OUTREACH:
        raise HTTPException(409, "Only a failed or interrupted outreach can be retried")

    line_count = db.scalar(select(func.count(ExcessLineItem.id)).where(ExcessLineItem.excess_list_id == el.id)) or 0
    subject = _neutral_outreach_subject(line_count)
    body = _RETRY_BODY
    # B36: the optimistic sending-flip is now a service function (thin-router discipline —
    # business logic lives in resell_outreach_service, not the router).
    resell_outreach_service.mark_outreach_retrying(db, row)

    background_tasks.add_task(
        resell_outreach_service.retry_outreach_send,
        outreach_id=outreach_id,
        owner_id=user.id,
        subject=subject,
        body=body,
        token=token,
    )
    el = excess_service.get_excess_list(db, list_id)
    return template_response("htmx/partials/resell/_outreach.html", _outreach_tracker_context(request, db, el, user))

"""resell_outreach_service.py — Outbound resell-outreach send/log + reply adapter.

The trader→buyer half of Resell (the inverse of sourcing's RFQ): the trader
proactively offers a posted ExcessList (or one line of it) to buyers, by email
(reusing the RFQ send engine) or by a manually-logged channel (phone/teams/
marketplace), and this service records each touch as an ExcessOutreach row and
ingests the buyers' replies.

Three entry points:
  - ``submit_outreach``        — manual-log path: create ExcessOutreach rows only
    (no email), one per (buyer × line) for ``per_line`` scope or one per buyer for
    ``whole_list``. Writes an ActivityLog (excess_list_id scope) per touch.
  - ``submit_outreach_email``  — email path: build the per-buyer payload, send via
    the ``send_batch_rfq`` adapter (DNC-at-send / save-to-sent / retry come free),
    stamp graph ids onto the rows, then ActivityLog per touch. Skipped recipients
    (no email / DNC) or send failures are recorded ``failed`` with the reason persisted in
    ``send_error`` — never silently dropped, never mislabeled buyer silence. Split into
    ``enqueue_outreach_email`` (SYNC — writes the rows in the transient ``sending``
    state and returns at once, so the modal never blocks on a multi-buyer send) +
    ``run_outreach_email_send`` (the BACKGROUND job the router enqueues — it performs
    the sends + per-buyer sent-message lookups off the request path and advances each
    row to ``sent`` / ``no_response``). ``submit_outreach_email`` itself is the inline
    convenience that runs both phases on one session (direct callers / tests).
  - ``record_response``        — reply adapter consumed by the inbox poll (or
    Chunk D): match a reply (conversation/message id) → the ExcessOutreach rows,
    advance ``status`` (responded → bid / declined), and link/create the inbound
    ExcessOffer when the reply carries one. Vendor-scoped like the RFQ path.

Plus ``counterparty_card`` — canonicalize a buyer (company_id XOR vendor_card_id)
to the single VendorCard "who" we score/dedup against, backfilling a card for a
company-only buyer on the shared ``normalize_vendor_name`` key.

ADDITIVE: reuses ``email_service.send_batch_rfq`` / ``_find_sent_message`` without
changing their signatures — see the resell-outreach Chunk B report for the
reuse-vs-wrapper boundary (graph ids require a second source-level lookup because
``send_batch_rfq`` only stamps them onto Contact rows, which need a requisition).

Called by: routers/resell.py (Chunk D wiring), the inbox poll adapter
Depends on: models (ExcessOutreach, ExcessOffer, ExcessList, VendorCard, Company),
            email_service, excess_service (can_post), activity_service, vendor_utils
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import (
    ActivityType,
    Channel,
    Direction,
    EventType,
    ExcessOfferScope,
    ExcessOfferStatus,
    ExcessOutreachChannel,
    ExcessOutreachStatus,
    OfferLineMatchStatus,
)
from ..database import SessionLocal
from ..models import ActivityLog, Company, User, VendorCard, VendorResponse
from ..models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine, ExcessOutreach
from ..utils.normalization import normalize_mpn_key
from ..vendor_utils import normalize_vendor_name
from .excess_service import can_post, get_excess_list

# Outreach scopes: a per-line campaign writes one row per (buyer × line); a
# whole-list campaign writes one row per buyer (excess_line_item_id stays NULL).
_SCOPE_PER_LINE = "per_line"
_SCOPE_WHOLE_LIST = "whole_list"

# Campaign-idempotency window: a buyer with a LIVE (sending/sent) row on the same
# (list, line) created within this window is skipped on a re-submit, so a double-click or
# a retried request never creates a second live row / second send. Generous enough to
# cover an in-flight background send, short enough that a genuine later re-offer is allowed.
_DUPLICATE_SUBMIT_WINDOW = timedelta(hours=1)


# ═══════════════════════════════════════════════════════════════════════
#  COUNTERPARTY CANONICALIZATION
# ═══════════════════════════════════════════════════════════════════════


def counterparty_card(
    db: Session,
    *,
    company_id: int | None = None,
    vendor_card_id: int | None = None,
) -> VendorCard:
    """Canonicalize a buyer to the single VendorCard "who" we score / dedup against.

    The buyer side carries the engagement / score columns on VendorCard (mirrored by
    BuyerScore), so a company-only buyer is backfilled to a card on the SHARED
    ``normalize_vendor_name`` key (the same key Company and VendorCard sync on). An
    existing card on that key is REUSED, never duplicated. ``vendor_card_id`` wins when
    both are given (it is already the canonical id). Raises ValueError if neither is
    supplied (callers must name a buyer) and HTTPException(404) for a dangling id.
    """
    if vendor_card_id is not None:
        card = db.get(VendorCard, vendor_card_id)
        if not card:
            raise HTTPException(404, f"VendorCard {vendor_card_id} not found")
        return card

    if company_id is None:
        raise ValueError("counterparty_card requires company_id or vendor_card_id")

    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(404, f"Company {company_id} not found")

    norm = company.normalized_name or normalize_vendor_name(company.name) or ""
    if not norm:
        raise HTTPException(422, f"Company {company_id} has no normalizable name to canonicalize")

    existing = db.query(VendorCard).filter(VendorCard.normalized_name == norm).first()
    if existing:
        return existing

    card = VendorCard(
        normalized_name=norm,
        display_name=company.name,
        domain=company.domain,
        emails=[],
        phones=[],
        source="resell_outreach_backfill",
    )
    db.add(card)
    db.flush()  # assign id for the outreach FK
    logger.info("Backfilled VendorCard id={} for company id={} ({!r})", card.id, company_id, company.name)
    return card


def _resolve_buyer_card(db: Session, buyer: dict) -> VendorCard:
    """Resolve one buyer dict ({vendor_card_id} | {company_id}) to its canonical
    card."""
    return counterparty_card(
        db,
        company_id=buyer.get("company_id"),
        vendor_card_id=buyer.get("vendor_card_id"),
    )


# ═══════════════════════════════════════════════════════════════════════
#  ROW BUILDING (shared by manual-log and email paths)
# ═══════════════════════════════════════════════════════════════════════


def _guard_owner(db: Session, list_id: int, owner: User) -> ExcessList:
    """Resolve the list and enforce the offering-out guards (owner + can_post).

    Offering excess OUT is the list owner's action: *owner* must both hold the
    sell-side ``can_post`` capability AND own the list. Mirrors ``submit_offer``'s
    guard discipline (raise HTTPException, never silent).
    """
    excess_list = get_excess_list(db, list_id)
    if not can_post(owner):
        raise HTTPException(403, "You do not have permission to offer out excess")
    if owner.id != excess_list.owner_id:
        raise HTTPException(403, "Only the list owner can offer it out")
    return excess_list


def _target_line_ids(
    db: Session, excess_list: ExcessList, scope: str, line_item_ids: list[int] | None
) -> list[int | None]:
    """The line ids to write one outreach row against, per buyer.

    ``whole_list`` → ``[None]`` (one row per buyer, no specific line). ``per_line`` →
    one entry per selected line (or every line on the list when ``line_item_ids`` is
    omitted). Raises HTTPException for an unknown scope or a line not on the list.
    """
    if scope == _SCOPE_WHOLE_LIST:
        return [None]
    if scope != _SCOPE_PER_LINE:
        raise HTTPException(422, f"Unknown outreach scope: {scope!r}")

    all_ids = [li.id for li in db.query(ExcessLineItem).filter_by(excess_list_id=excess_list.id).all()]
    if line_item_ids is None:
        return list(all_ids)
    on_list = set(all_ids)
    bad = [lid for lid in line_item_ids if lid not in on_list]
    if bad:
        raise HTTPException(422, f"Line item(s) {bad} are not on list {excess_list.id}")
    return list(line_item_ids)


def _parts_snapshot(db: Session, excess_list: ExcessList, line_id: int | None) -> list[dict]:
    """The offered-lines snapshot stored on ExcessOutreach.parts_included.

    One entry for the specific line, or the whole list for a whole-list touch.
    """
    q = db.query(ExcessLineItem).filter_by(excess_list_id=excess_list.id)
    if line_id is not None:
        q = q.filter(ExcessLineItem.id == line_id)
    return [{"part_number": li.part_number, "quantity": li.quantity, "line_item_id": li.id} for li in q.all()]


def _has_live_recent_outreach(
    db: Session, *, list_id: int, card_id: int, line_id: int | None, cutoff: datetime
) -> bool:
    """True if this buyer already has a LIVE (sending/sent) outreach on the same (list,
    line) created since ``cutoff`` — the campaign-idempotency dedup check.

    ``line_id`` NULL matches the whole-list touch (``excess_line_item_id IS NULL``). Uses
    the SENDING/SENT filter (mirroring the finalize idempotency guard) so a failed /
    interrupted / no_response prior touch never blocks a genuine re-offer.
    """
    conds = [
        ExcessOutreach.excess_list_id == list_id,
        ExcessOutreach.target_vendor_card_id == card_id,
        ExcessOutreach.status.in_([ExcessOutreachStatus.SENDING, ExcessOutreachStatus.SENT]),
        ExcessOutreach.created_at >= cutoff,
        ExcessOutreach.excess_line_item_id.is_(None)
        if line_id is None
        else ExcessOutreach.excess_line_item_id == line_id,
    ]
    return db.scalar(select(ExcessOutreach.id).where(*conds).limit(1)) is not None


def _make_outreach_rows(
    db: Session,
    *,
    excess_list: ExcessList,
    owner: User,
    card: VendorCard,
    channel: str,
    line_ids: list[int | None],
    status: str,
    sent_at: datetime | None = None,
) -> list[ExcessOutreach]:
    """Create (and flush) one ExcessOutreach row per line id for a single buyer."""
    rows: list[ExcessOutreach] = []
    for line_id in line_ids:
        row = ExcessOutreach(
            excess_list_id=excess_list.id,
            excess_line_item_id=line_id,
            target_vendor_card_id=card.id,
            submitted_by=owner.id,
            channel=channel,
            status=status,
            parts_included=_parts_snapshot(db, excess_list, line_id),
            sent_at=sent_at,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def _log_outreach_activity(
    db: Session,
    *,
    owner: User,
    excess_list: ExcessList,
    card: VendorCard,
    channel: str,
    sent: bool,
    notes: str | None = None,
) -> None:
    """Write one outbound ActivityLog (excess_list_id scope) for an outreach touch.

    Reuses the shared immutable timeline + cadence clocks via the activity service's
    cadence bump, keyed on the canonical buyer vendor card. ``notes`` (the trader's
    free-text on a manual-log touch — "left a voicemail", a marketplace thread url) is
    recorded on ``ActivityLog.notes`` so it lands on the immutable timeline rather than
    being silently dropped.
    """
    verb = "Emailed" if channel == ExcessOutreachChannel.EMAIL else f"{channel.title()} to"
    record = ActivityLog(
        user_id=owner.id,
        activity_type=ActivityType.EMAIL_SENT if sent else ActivityType.NOTE,
        channel=Channel.EMAIL if channel == ExcessOutreachChannel.EMAIL else Channel.MANUAL,
        direction=Direction.OUTBOUND,
        event_type=EventType.EMAIL if channel == ExcessOutreachChannel.EMAIL else None,
        excess_list_id=excess_list.id,
        vendor_card_id=card.id,
        contact_name=card.display_name,
        subject=f"{verb} {card.display_name}: excess offer ({excess_list.title})",
        notes=notes,
        is_meaningful=True,
        auto_logged=True,
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)


# ═══════════════════════════════════════════════════════════════════════
#  SUBMIT — manual-log path
# ═══════════════════════════════════════════════════════════════════════


def submit_outreach(
    db: Session,
    *,
    list_id: int,
    owner: User,
    buyers: list[dict],
    scope: str,
    channel: str,
    send_email: bool = False,
    line_item_ids: list[int] | None = None,
    notes: str | None = None,
) -> list[ExcessOutreach]:
    """Record a manually-logged outreach (phone / teams / marketplace / other).

    Guards: *owner* must hold ``can_post`` AND own the list (offering out is the
    owner's action). Each buyer ({vendor_card_id} | {company_id}) is canonicalized to a
    VendorCard via :func:`counterparty_card` (a company-only buyer is backfilled). For
    ``scope='per_line'`` one ExcessOutreach row is written per (buyer × line); for
    ``scope='whole_list'`` one row per buyer. Each row is ``status=sent``,
    ``submitted_by=owner``, carries the offered-lines ``parts_included`` snapshot, and
    gets one outbound ActivityLog (excess_list_id scope). Commits. Returns the rows.

    ``send_email`` must be False here — the email path is :func:`submit_outreach_email`
    (it is async; this sync entry point is the log-only path). ``notes`` is written to
    each touch's ``ActivityLog.notes`` (the immutable timeline). Raises HTTPException on
    guard / validation failure.
    """
    if send_email:
        raise ValueError("submit_outreach is the manual-log path; use submit_outreach_email for email")
    if channel == ExcessOutreachChannel.EMAIL:
        raise HTTPException(422, "channel='email' must go through submit_outreach_email")

    excess_list = _guard_owner(db, list_id, owner)
    channel_value = ExcessOutreachChannel(channel).value  # raises ValueError on a bad channel
    line_ids = _target_line_ids(db, excess_list, scope, line_item_ids)

    all_rows: list[ExcessOutreach] = []
    for buyer in buyers:
        card = _resolve_buyer_card(db, buyer)
        rows = _make_outreach_rows(
            db,
            excess_list=excess_list,
            owner=owner,
            card=card,
            channel=channel_value,
            line_ids=line_ids,
            status=ExcessOutreachStatus.SENT,
        )
        all_rows.extend(rows)
        _log_outreach_activity(
            db, owner=owner, excess_list=excess_list, card=card, channel=channel_value, sent=False, notes=notes
        )

    db.commit()
    for row in all_rows:
        db.refresh(row)
    logger.info(
        "Logged {} outreach row(s) on list={} ({} buyer(s), channel={}) by owner={}",
        len(all_rows),
        list_id,
        len(buyers),
        channel_value,
        owner.id,
    )
    return all_rows


# ═══════════════════════════════════════════════════════════════════════
#  SUBMIT — email path (send_batch_rfq adapter)
# ═══════════════════════════════════════════════════════════════════════


def enqueue_outreach_email(
    db: Session,
    *,
    list_id: int,
    owner: User,
    buyers: list[dict],
    scope: str,
    subject: str,
    body: str,
    line_item_ids: list[int] | None = None,
) -> tuple[list[ExcessOutreach], list[dict]]:
    """Phase 1 (SYNC, request path): write the tracker rows in the transient ``sending``
    state + build a serializable send plan, WITHOUT touching Graph.

    Guards identical to :func:`submit_outreach`. Each buyer ({vendor_card_id} |
    {company_id} + optional ``email`` override) is canonicalized to a VendorCard and gets
    one ExcessOutreach row per (buyer × line) for ``per_line`` or per buyer for
    ``whole_list``, all ``status=sending`` with no graph ids yet. Commits so the tracker
    re-render shows them immediately. Returns ``(rows, plan)`` where ``plan`` is a list of
    one group per buyer — ``{card_id, email, row_ids, parts}`` — that the router hands to
    :func:`run_outreach_email_send` as a FastAPI ``BackgroundTask`` so the actual send +
    per-buyer sent-message lookups never block the modal.
    """
    excess_list = _guard_owner(db, list_id, owner)
    line_ids = _target_line_ids(db, excess_list, scope, line_item_ids)
    cutoff = datetime.now(UTC) - _DUPLICATE_SUBMIT_WINDOW

    all_rows: list[ExcessOutreach] = []
    plan: list[dict] = []
    for buyer in buyers:
        card = _resolve_buyer_card(db, buyer)
        email = buyer.get("email") or _primary_email(card)
        # Campaign idempotency: drop the line ids this buyer already has a LIVE
        # (sending/sent) row for within the dedup window, so a re-submit makes no duplicate
        # row / send. A buyer with nothing fresh left is skipped entirely.
        fresh_line_ids = [
            lid
            for lid in line_ids
            if not _has_live_recent_outreach(db, list_id=excess_list.id, card_id=card.id, line_id=lid, cutoff=cutoff)
        ]
        if not fresh_line_ids:
            logger.info(
                "Outreach enqueue: buyer card={} already has a live offer on list={} — skipping duplicate",
                card.id,
                excess_list.id,
            )
            continue
        # Offered-parts snapshot for THIS buyer's fresh lines (drives the email body).
        parts = [p["part_number"] for lid in fresh_line_ids for p in _parts_snapshot(db, excess_list, lid)]
        rows = _make_outreach_rows(
            db,
            excess_list=excess_list,
            owner=owner,
            card=card,
            channel=ExcessOutreachChannel.EMAIL,
            line_ids=fresh_line_ids,
            status=ExcessOutreachStatus.SENDING,
        )
        all_rows.extend(rows)
        plan.append({"card_id": card.id, "email": email, "row_ids": [r.id for r in rows], "parts": parts})

    db.commit()
    for row in all_rows:
        db.refresh(row)
    logger.info(
        "Enqueued {} outreach email row(s) on list={} ({} buyer(s)) by owner={} — sending in background",
        len(all_rows),
        list_id,
        len(buyers),
        owner.id,
    )
    return all_rows, plan


async def _finalize_outreach_send(
    db: Session,
    *,
    excess_list: ExcessList,
    owner: User,
    subject: str,
    body: str,
    token: str,
    plan: list[dict],
) -> list[ExcessOutreach]:
    """Send the emails, stamp graph ids, and advance each ``sending`` row to its final
    status. Shared by :func:`submit_outreach_email` (inline) and
    :func:`run_outreach_email_send` (background) — the ONE place the send + lookup live.

    Reuses the RFQ send engine in its no-requisition mode (email out, no Contact rows;
    the live RFQ tracking path is untouched). Graph ids do NOT come back in
    ``send_batch_rfq``'s result (it only stamps them onto Contact rows), so for each SENT
    buyer we reuse ``email_service._find_sent_message`` — the SAME source-level lookup —
    to fetch the just-sent message's ids. A skipped recipient (no email / DNC), a
    per-buyer send error, or a total send outage is recorded ``failed`` with the reason
    persisted in ``send_error`` (NEVER ``no_response`` — that state is genuine buyer
    silence only) — never silently dropped, never stuck ``sending``. A delivered row whose
    Graph-id lookup came back empty stays ``sent`` but carries a degraded note in
    ``send_error``. Idempotent: only rows still in ``sending`` are sent, so re-running the
    plan never double-sends.

    Commit boundary: the send OUTCOME (status + graph ids) is committed here, immediately
    after the send, BEFORE any bookkeeping — so a later activity/cadence write error can
    never roll back a delivered ``sent``. The per-buyer bookkeeping (an "Emailed"
    ActivityLog + cadence bump, SENT buyers only) is then committed independently and
    guarded. Returns the finalized rows.
    """
    from app import email_service
    from app.utils.graph_client import GraphClient

    # Idempotency guard: resolve each group's rows and keep only the ones still in
    # ``sending`` (a re-run after a partial finalize must not re-send an already-sent
    # buyer). A group with no live rows is dropped from the send entirely.
    pending: list[tuple[VendorCard | None, str | None, list[ExcessOutreach]]] = []
    vendor_groups: list[dict] = []
    for group in plan:
        rows = [db.get(ExcessOutreach, rid) for rid in group["row_ids"]]
        live = [r for r in rows if r is not None and r.status == ExcessOutreachStatus.SENDING]
        if not live:
            continue
        card = db.get(VendorCard, group["card_id"])
        email = group["email"]
        pending.append((card, email, live))
        vendor_groups.append(
            {
                "vendor_name": card.display_name if card else "",
                "vendor_email": email or "",
                "parts": group.get("parts", []),
                "subject": subject,
                "body": body,
            }
        )

    if not pending:
        return []

    # A total send outage flags EVERY pending buyer ``failed`` (not ``no_response`` — the
    # buyer was never contacted, so it is NOT silence) with this exception text persisted
    # as the reason; the rows are kept + retryable, never stranded in ``sending``.
    total_error: str | None = None
    try:
        send_results = await email_service.send_batch_rfq(
            token=token,
            db=db,
            user_id=owner.id,
            requisition_id=None,
            vendor_groups=vendor_groups,
        )
    except Exception as exc:
        logger.exception("Outreach send_batch_rfq raised for list={} — flagging pending rows failed", excess_list.id)
        send_results = []
        total_error = f"{type(exc).__name__}: {exc}"
    # Index results by recipient email (send_batch_rfq preserves vendor identity in each
    # result dict) so we can map a per-buyer outcome back to its rows.
    result_by_email: dict[str, dict] = {(r.get("vendor_email") or "").lower(): r for r in send_results}

    gc = GraphClient(token)
    send_time = datetime.now(UTC)
    finalized: list[ExcessOutreach] = []
    # Buyers whose send SUCCEEDED — the only ones that get post-send bookkeeping (an
    # "Emailed" ActivityLog + a cadence bump). Gated here at the call site (NOT inside
    # ``_log_outreach_activity``, whose manual-log path legitimately passes sent=False) so
    # a FAILED send never writes an "Emailed" touch nor advances the cadence clocks.
    sent_cards: list[VendorCard | None] = []
    for card, email, rows in pending:
        result = result_by_email.get((email or "").lower(), {})
        sent_ok = result.get("status") == "sent"
        if sent_ok:
            # A clean send: SENT, clear any prior error, stamp the send time.
            for row in rows:
                row.status = ExcessOutreachStatus.SENT
                row.sent_at = send_time
                row.send_error = None
        else:
            # The send never reached the buyer → ``failed`` with the persisted reason
            # (the per-buyer skip/error string, or the total-outage text, else a generic
            # fallback). NEVER ``no_response`` — that state is genuine buyer silence only.
            send_error = result.get("error") or total_error or "send did not complete (no send result for recipient)"
            for row in rows:
                row.status = ExcessOutreachStatus.FAILED
                row.sent_at = None
                row.send_error = send_error

        # Stamp graph ids on the SENT buyer's rows via the same source-level lookup
        # send_batch_rfq uses internally (we cannot get them from the result dict).
        if sent_ok and email:
            try:
                sent_msg = await email_service._find_sent_message(gc, subject, email)
            except Exception:  # lookup is best-effort — a failure must not lose the row
                logger.warning("Outreach sent-message lookup failed for <{}>", email, exc_info=True)
                sent_msg = None
            if isinstance(sent_msg, dict) and sent_msg:
                for row in rows:
                    row.graph_message_id = sent_msg.get("id")
                    row.graph_conversation_id = sent_msg.get("conversationId")
            else:
                # Delivered but the reply-matching id lookup came back empty — flag it
                # (on the SENT row's send_error) so the tracker shows "delivered, reply-
                # matching degraded" instead of a silent clean send.
                logger.warning(
                    "Outreach graph ids left NULL for buyer '{}' <{}> — reply matching degrades",
                    card.display_name if card else "?",
                    email,
                )
                for row in rows:
                    row.send_error = "delivered; reply-matching degraded (no Graph message id)"

        finalized.extend(rows)
        if sent_ok:
            sent_cards.append(card)

    # Persist the send OUTCOME (status + graph ids) BEFORE any bookkeeping, so a
    # bookkeeping failure below can never roll back a delivered SENT (finding #6 — the old
    # blanket except→rollback could revert a row whose email had already gone out).
    db.commit()

    # Post-send bookkeeping: an "Emailed" ActivityLog + cadence bump per SENT buyer only.
    # Each is guarded + committed independently — a write error is logged, never fatal, and
    # never reverts the already-committed send outcome above.
    for card in sent_cards:
        if card is None:
            continue
        try:
            _log_outreach_activity(
                db,
                owner=owner,
                excess_list=excess_list,
                card=card,
                channel=ExcessOutreachChannel.EMAIL,
                sent=True,
            )
            db.commit()
        except Exception:
            logger.exception(
                "Outreach post-send bookkeeping failed for card={} on list={} — send already committed",
                card.id if card else None,
                excess_list.id,
            )
            db.rollback()

    return finalized


async def run_outreach_email_send(
    *,
    list_id: int,
    owner_id: int,
    subject: str,
    body: str,
    token: str,
    groups: list[dict],
    session_factory=None,
) -> None:
    """Phase 2 (BACKGROUND job the router enqueues): perform the sends + per-buyer sent-
    message lookups off the request path and advance each ``sending`` row.

    Opens its OWN session — the request session is already closed by the time a FastAPI
    ``BackgroundTask`` runs — via ``session_factory`` (defaults to the app ``SessionLocal``;
    injectable so tests can bind it to the test session). Reloads the owner + list, runs
    :func:`_finalize_outreach_send` over ``groups`` (the plan from
    :func:`enqueue_outreach_email`). ``_finalize_outreach_send`` owns the commit boundaries
    (it commits the send outcome before bookkeeping), so the delivered ``sent`` is durable
    by the time this returns; the outer guard here only catches a pre-outcome setup error
    (owner/list load) — it can no longer roll back a delivered send. Idempotent (only
    ``sending`` rows are sent) and self-contained (own try/rollback/close) so a failure can
    never poison a request. Returns nothing — the tracker's ``sending`` poll surfaces the
    final state.
    """
    factory = session_factory or SessionLocal
    db = factory()
    try:
        owner = db.get(User, owner_id)
        excess_list = db.get(ExcessList, list_id)
        if owner is None or excess_list is None:
            logger.error("Outreach send job: owner={} or list={} missing — aborting", owner_id, list_id)
            return
        await _finalize_outreach_send(
            db, excess_list=excess_list, owner=owner, subject=subject, body=body, token=token, plan=groups
        )
        logger.info("Background outreach send finished for list={} by owner={}", list_id, owner_id)
    except Exception:
        # The send outcome is already committed inside _finalize_outreach_send; this only
        # discards an uncommitted pre-outcome setup failure — never a delivered send.
        logger.exception("Background outreach send failed for list={}", list_id)
        db.rollback()
    finally:
        db.close()


async def submit_outreach_email(
    db: Session,
    *,
    list_id: int,
    owner: User,
    buyers: list[dict],
    scope: str,
    token: str,
    subject: str,
    body: str,
    line_item_ids: list[int] | None = None,
) -> list[ExcessOutreach]:
    """Send an outreach email per buyer (reusing send_batch_rfq), then track + stamp.

    Inline convenience that runs both phases on ONE session:
    :func:`enqueue_outreach_email` (write the ``sending`` rows + build the plan) then
    :func:`_finalize_outreach_send` (send + stamp + advance to ``sent`` / ``no_response``).
    Direct callers / tests that want the fully-finalized rows in one call use this; the
    ROUTER instead enqueues the finalize as a background job (via
    :func:`run_outreach_email_send`) so the modal returns immediately. Commits. Returns
    the rows.
    """
    rows, plan = enqueue_outreach_email(
        db,
        list_id=list_id,
        owner=owner,
        buyers=buyers,
        scope=scope,
        subject=subject,
        body=body,
        line_item_ids=line_item_ids,
    )
    excess_list = get_excess_list(db, list_id)
    await _finalize_outreach_send(
        db, excess_list=excess_list, owner=owner, subject=subject, body=body, token=token, plan=plan
    )
    db.commit()
    for row in rows:
        db.refresh(row)
    logger.info(
        "Sent {} outreach email row(s) on list={} ({} buyer(s)) by owner={}",
        len(rows),
        list_id,
        len(buyers),
        owner.id,
    )
    return rows


def _primary_email(card: VendorCard) -> str | None:
    """First usable email on a vendor card (the buyer's send address), or None."""
    for e in card.emails or []:
        if e and "@" in e:
            email: str = e.strip()  # JSON column holds str emails
            return email
    return None


# ═══════════════════════════════════════════════════════════════════════
#  RETRY + STALE-SENDING SWEEPER (send durability)
# ═══════════════════════════════════════════════════════════════════════

# States a failed send can be retried FROM. ``sending`` is included so the retry route
# can optimistically flip the row to ``sending`` (for the tracker poll) before the
# background retry runs, and a direct retry of a ``failed`` / ``interrupted`` row works too.
_RETRYABLE_STATUSES = {
    ExcessOutreachStatus.FAILED,
    ExcessOutreachStatus.INTERRUPTED,
    ExcessOutreachStatus.SENDING,
}

# A ``sending`` row this old is presumed orphaned (its background send job died mid-flight)
# — the nightly sweeper flips it to ``interrupted`` so it stops polling and becomes
# retryable. Generous enough to never race an in-flight multi-buyer send.
_STALE_SENDING_MINUTES = 30


async def retry_outreach_send(
    *,
    outreach_id: int,
    owner_id: int,
    subject: str,
    body: str,
    token: str,
    session_factory=None,
) -> None:
    """Retry a ``failed`` / ``interrupted`` outreach row — reconcile-first, resend only
    if the original never actually went out (the double-send guard).

    Opens its OWN session (a background job, mirroring :func:`run_outreach_email_send`). A
    row not in a retryable state is skipped (idempotent). The buyer email is resolved, then
    — CRITICALLY — the Sent folder is re-checked via ``email_service._find_sent_message``
    BEFORE any resend: a ``failed`` row may have actually DELIVERED (the failure was
    downstream of the send), so if the message is already there the row is reconciled to
    ``sent`` + its graph ids stamped and NOTHING is resent. Only when the reconcile finds
    nothing is the row reset to ``sending`` and re-sent via :func:`_finalize_outreach_send`
    (a one-buyer plan built from the row's ``parts_included`` snapshot). Own
    commit/rollback/close.
    """
    from app import email_service
    from app.utils.graph_client import GraphClient

    factory = session_factory or SessionLocal
    db = factory()
    try:
        row = db.get(ExcessOutreach, outreach_id)
        if row is None:
            logger.error("Outreach retry: row={} missing — aborting", outreach_id)
            return
        if row.status not in _RETRYABLE_STATUSES:
            logger.info("Outreach retry: row={} status={} not retryable — skipping", outreach_id, row.status)
            return
        owner = db.get(User, owner_id)
        excess_list = db.get(ExcessList, row.excess_list_id)
        if owner is None or excess_list is None:
            logger.error("Outreach retry: owner={} or list={} missing — aborting", owner_id, row.excess_list_id)
            return
        card = db.get(VendorCard, row.target_vendor_card_id) if row.target_vendor_card_id else None
        email = _primary_email(card) if card else None
        if not email:
            row.status = ExcessOutreachStatus.FAILED
            row.send_error = "no buyer email on file to retry"
            row.sent_at = None
            db.commit()
            logger.warning("Outreach retry: row={} has no buyer email — left failed", outreach_id)
            return

        # Double-send guard: re-run the Sent-folder lookup BEFORE resending. A ``failed``
        # row may have actually delivered, so never assume the send did not happen.
        gc = GraphClient(token)
        try:
            sent_msg = await email_service._find_sent_message(gc, subject, email)
        except Exception:  # best-effort — a lookup failure falls through to a resend
            logger.warning("Outreach retry sent-lookup failed for <{}>", email, exc_info=True)
            sent_msg = None
        if isinstance(sent_msg, dict) and sent_msg:
            # Already in Sent — the original delivered; reconcile, DO NOT resend.
            row.status = ExcessOutreachStatus.SENT
            row.sent_at = row.sent_at or datetime.now(UTC)
            row.send_error = None
            row.graph_message_id = sent_msg.get("id")
            row.graph_conversation_id = sent_msg.get("conversationId")
            db.commit()
            logger.info("Outreach retry: row={} already delivered — reconciled to sent, not resent", outreach_id)
            return

        # Not delivered — reset to ``sending`` and re-send via the shared finalize (which
        # sends the SENDING row, stamps ids, and commits the send outcome).
        row.status = ExcessOutreachStatus.SENDING
        row.sent_at = None
        row.send_error = None
        db.commit()
        parts = [p.get("part_number") for p in (row.parts_included or []) if p.get("part_number")]
        plan = [{"card_id": card.id, "email": email, "row_ids": [row.id], "parts": parts}]
        await _finalize_outreach_send(
            db, excess_list=excess_list, owner=owner, subject=subject, body=body, token=token, plan=plan
        )
        logger.info("Outreach retry: row={} resent (was not in Sent)", outreach_id)
    except Exception:
        logger.exception("Outreach retry failed for row={}", outreach_id)
        db.rollback()
    finally:
        db.close()


def sweep_stale_sending_outreach(db: Session, *, now: datetime | None = None) -> int:
    """Flip every outreach row stuck in ``sending`` past the staleness threshold to
    ``interrupted`` — the durability backstop for a background send job that died.

    A row optimistically written ``sending`` whose finalize job crashed (or the process
    was killed) mid-flight would otherwise poll forever. This nightly sweep flips such aged
    rows to ``interrupted`` — the ambiguous "we don't know if it sent" state, NEVER
    ``no_response`` (that would libel the buyer as contacted-and-silent) and NEVER a resend
    (whether the send actually landed is unknown here; the manual retry path does the
    Sent-folder lookup before any resend). Idempotent, commits once, returns the count
    flipped.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(minutes=_STALE_SENDING_MINUTES)
    stale = db.scalars(
        select(ExcessOutreach).where(
            ExcessOutreach.status == ExcessOutreachStatus.SENDING,
            ExcessOutreach.created_at < cutoff,
        )
    ).all()
    for row in stale:
        row.status = ExcessOutreachStatus.INTERRUPTED
        row.send_error = (
            "send interrupted — stuck in 'sending' past the staleness threshold (background job did not finalize)"
        )
    if stale:
        db.commit()
    logger.info("Swept {} stale 'sending' outreach row(s) to 'interrupted'", len(stale))
    return len(stale)


# ═══════════════════════════════════════════════════════════════════════
#  REPLY ADAPTER
# ═══════════════════════════════════════════════════════════════════════


def record_response(
    db: Session,
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
    has_offer: bool = False,
    declined: bool = False,
    offer_lines: list[dict] | None = None,
    offer_notes: str | None = None,
    commit: bool = True,
) -> list[ExcessOutreach]:
    """Advance the matched ExcessOutreach row(s) on a buyer's reply; link an offer.

    Adapter consumed by the inbox poll (Chunk D) or called directly. Matches the reply
    to ExcessOutreach rows by ``graph_conversation_id`` (preferred — the whole thread,
    like RFQ Tier-1) then ``graph_message_id``. For each matched row, advances
    ``status``: ``bid`` when the reply carries an offer, ``declined`` when the buyer
    passed, else ``responded`` — but a row already in a terminal state (``bid`` /
    ``declined``) is never regressed by a late generic reply (mirrors
    ``_progress_contact_status``).

    When ``has_offer`` and ``offer_lines`` are given, creates ONE inbound ExcessOffer
    scoped to the canonical buyer (``offerer_vendor_card_id`` from the outreach's
    ``target_vendor_card_id``), with one ExcessOfferLine per row matched to the list's
    lines by part number only (the same matching ``submit_offer`` uses) — unmatched rows
    are queued, never dropped. Returns the advanced rows ([] if no match).

    ``commit`` (default True) commits + refreshes. Pass ``commit=False`` when the caller
    owns the transaction (the inbox poll runs this inside a per-message savepoint): the
    status changes + any linked ExcessOffer/Line are still ``flush``ed so they get PKs and
    are visible in the session, but the enclosing txn stays open for the caller to commit.

    Raises ValueError if neither conversation_id nor message_id is supplied.
    """
    if not conversation_id and not message_id:
        raise ValueError("record_response requires conversation_id or message_id")

    rows = _match_outreach(db, conversation_id=conversation_id, message_id=message_id)
    if not rows:
        logger.info("record_response: no ExcessOutreach matched (conv={!r} msg={!r})", conversation_id, message_id)
        return []

    if has_offer:
        new_status = ExcessOutreachStatus.BID
    elif declined:
        new_status = ExcessOutreachStatus.DECLINED
    else:
        new_status = ExcessOutreachStatus.RESPONDED

    _terminal = {ExcessOutreachStatus.BID, ExcessOutreachStatus.DECLINED}
    for row in rows:
        if row.status in _terminal:
            continue  # never regress a buyer who already bid / declined
        row.status = new_status

    if has_offer:
        _link_inbound_offer(db, rows[0], offer_lines or [], offer_notes)

    if commit:
        db.commit()
        for row in rows:
            db.refresh(row)
    else:
        # Caller owns the txn (inbox-poll savepoint): flush so status changes + the
        # linked ExcessOffer/Line get PKs and are visible before the caller commits.
        db.flush()
    logger.info("record_response advanced {} outreach row(s) → {} (offer={})", len(rows), new_status, has_offer)
    return rows


def _log_inbound_reply_activity(db: Session, *, outreach: ExcessOutreach, vr: VendorResponse) -> None:
    """Write one inbound ActivityLog for a buyer's reply on a resell outreach.

    Sibling of :func:`_log_outreach_activity` for the INBOUND leg: the buyer's reply lands
    on the same immutable timeline + cadence clocks, scoped to the outreach's
    ``excess_list_id`` and its canonical buyer ``vendor_card_id``. Idempotent per reply —
    dedups on ``external_id`` (the Graph message id) within the list scope, so a per-line
    campaign whose rows share one conversation logs the reply ONCE (never once-per-line),
    and it never collides with the requisition-side ``log_email_activity`` row (that row
    carries no ``excess_list_id``). Advances the reply clock via ``bump_clocks_from_activity``.
    """
    if vr.message_id:
        existing = (
            db.query(ActivityLog)
            .filter(
                ActivityLog.external_id == vr.message_id,
                ActivityLog.excess_list_id == outreach.excess_list_id,
            )
            .first()
        )
        if existing:
            return

    card = db.get(VendorCard, outreach.target_vendor_card_id) if outreach.target_vendor_card_id else None
    record = ActivityLog(
        user_id=outreach.submitted_by,
        activity_type=ActivityType.EMAIL_RECEIVED,
        channel=Channel.EMAIL,
        direction=Direction.INBOUND,
        event_type=EventType.EMAIL,
        excess_list_id=outreach.excess_list_id,
        vendor_card_id=outreach.target_vendor_card_id,
        contact_name=card.display_name if card else vr.vendor_name,
        contact_email=vr.vendor_email,
        subject=vr.subject,
        external_id=vr.message_id,
        is_meaningful=True,
        auto_logged=True,
    )
    db.add(record)
    db.flush()

    from .cadence_service import bump_clocks_from_activity

    bump_clocks_from_activity(db, record)


def _match_outreach(
    db: Session,
    *,
    conversation_id: str | None,
    message_id: str | None,
) -> list[ExcessOutreach]:
    """Match a reply to outreach rows — conversation id (whole thread) then message id.

    Conversation id is the preferred key (matches all rows on the thread, like RFQ
    Tier-1 fan-out); message id is the exact-touch fallback. Vendor-scoped by
    construction: a conversation id is unique to one buyer's send.
    """
    if conversation_id:
        rows = db.query(ExcessOutreach).filter(ExcessOutreach.graph_conversation_id == conversation_id).all()
        if rows:
            return rows
    if message_id:
        return db.query(ExcessOutreach).filter(ExcessOutreach.graph_message_id == message_id).all()
    return []


def _link_inbound_offer(
    db: Session,
    outreach: ExcessOutreach,
    offer_lines: list[dict],
    notes: str | None,
) -> ExcessOffer:
    """Create the inbound ExcessOffer a reply carries, scoped to the canonical buyer.

    The buyer replying to outreach is NOT a User (so ``excess_service.submit_offer``,
    which is User-driven and blocks self-offers, does not fit) — the offer is keyed to
    the buyer's canonical VendorCard (``offerer_vendor_card_id`` = the outreach's
    ``target_vendor_card_id``). Line matching reuses the SAME normalize_mpn_key
    part-number-only matching as ``submit_offer``: exactly one match → ``matched`` +
    ``excess_line_item_id``; none → ``unmatched``; many → ``ambiguous`` (queued, never
    dropped). ``submitted_by`` records the list owner (the inbound offer was solicited
    by them). Flushes; the caller commits.
    """
    excess_list = db.get(ExcessList, outreach.excess_list_id)

    # An inbound reply landing after the posting window closed is flagged ``late`` (never
    # dropped) — same rule as the User-driven submit_offer path.
    from .excess_service import offer_status_for_list

    status = offer_status_for_list(excess_list.status) if excess_list is not None else ExcessOfferStatus.OPEN
    offer = ExcessOffer(
        excess_list_id=outreach.excess_list_id,
        submitted_by=outreach.submitted_by,
        offerer_vendor_card_id=outreach.target_vendor_card_id,
        scope=ExcessOfferScope.PER_LINE,
        status=status,
        notes=notes,
    )
    db.add(offer)
    db.flush()

    # Index the list's lines by normalized part number (same shape as submit_offer).
    by_norm: dict[str, list[ExcessLineItem]] = {}
    if excess_list is not None:
        for li in db.query(ExcessLineItem).filter_by(excess_list_id=excess_list.id).all():
            key = li.normalized_part_number or normalize_mpn_key(li.part_number)
            if key:
                by_norm.setdefault(key, []).append(li)

    affected: set[int] = set()
    for row in offer_lines:
        mpn_raw = (row.get("mpn_raw") or "").strip()
        norm_key = normalize_mpn_key(mpn_raw)
        candidates = by_norm.get(norm_key, []) if norm_key else []
        if len(candidates) == 1:
            match_status = OfferLineMatchStatus.MATCHED
            matched_id = candidates[0].id
            affected.add(matched_id)
        elif len(candidates) > 1:
            match_status = OfferLineMatchStatus.AMBIGUOUS
            matched_id = None
        else:
            match_status = OfferLineMatchStatus.UNMATCHED
            matched_id = None

        db.add(
            ExcessOfferLine(
                offer_id=offer.id,
                excess_line_item_id=matched_id,
                mpn_raw=mpn_raw,
                quantity=row.get("quantity") or 1,
                unit_price=_as_decimal(row.get("unit_price")),
                lead_time_days=row.get("lead_time_days"),
                terms_text=row.get("terms_text"),
                match_status=match_status,
            )
        )

    db.flush()
    # Recompute the best-price rollup for every line this offer touched (reuse).
    from .excess_service import notify_owner_of_offer, recompute_line_rollup

    for line_item_id in affected:
        recompute_line_rollup(db, line_item_id)

    # M6: notify the list owner a buyer reply carrying a bid landed (deduped per
    # (list, buyer)). The buyer here is the canonical VendorCard, not a User.
    if excess_list is not None:
        card = db.get(VendorCard, outreach.target_vendor_card_id) if outreach.target_vendor_card_id else None
        buyer_ref = (
            f"card-{outreach.target_vendor_card_id}"
            if outreach.target_vendor_card_id
            else f"user-{outreach.submitted_by}"
        )
        buyer_label = (card.display_name if card else None) or "a buyer"
        notify_owner_of_offer(
            db,
            excess_list=excess_list,
            activity_type=ActivityType.BID_RECEIVED,
            buyer_ref=buyer_ref,
            buyer_label=buyer_label,
            vendor_card_id=outreach.target_vendor_card_id,
        )

    logger.info(
        "Linked inbound ExcessOffer id={} (buyer card={}) from outreach id={} ({} matched lines)",
        offer.id,
        outreach.target_vendor_card_id,
        outreach.id,
        len(affected),
    )
    return offer


def _as_decimal(value) -> Decimal | None:
    """Coerce a price input to Decimal (None on blank/invalid)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None

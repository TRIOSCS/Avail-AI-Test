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
    (no email / DNC) are recorded ``no_response`` — never silently dropped.
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

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from loguru import logger
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
from ..models import ActivityLog, Company, User, VendorCard, VendorResponse
from ..models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine, ExcessOutreach
from ..utils.normalization import normalize_mpn_key
from ..vendor_utils import normalize_vendor_name
from .excess_service import can_post, get_excess_list

# Outreach scopes: a per-line campaign writes one row per (buyer × line); a
# whole-list campaign writes one row per buyer (excess_line_item_id stays NULL).
_SCOPE_PER_LINE = "per_line"
_SCOPE_WHOLE_LIST = "whole_list"


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

    Guards identical to :func:`submit_outreach`. Each buyer ({vendor_card_id} |
    {company_id} + optional ``email`` override) is canonicalized to a VendorCard. The
    adapter builds one ``vendor_groups`` entry per buyer (buyer as the recipient, the
    excess lines as the parts) and hands them to ``email_service.send_batch_rfq`` with
    NO requisition — so the email goes out with DNC-at-send / save-to-sent / retry, but
    no RFQ Contact rows are written (the live RFQ tracking path is untouched). We do our
    OWN tracking: one ExcessOutreach row per (buyer × line) for ``per_line`` or per
    buyer for ``whole_list``.

    Graph ids do NOT come back in ``send_batch_rfq``'s result (it only stamps them onto
    Contact rows), so for each SENT buyer we reuse ``email_service._find_sent_message``
    — the SAME source-level lookup send_batch_rfq uses — to fetch the just-sent
    message's ids and stamp them onto the buyer's rows. A skipped recipient (no email /
    DNC, surfaced by send_batch_rfq) is recorded ``status=no_response`` — never silently
    dropped. Commits. Returns the rows.
    """
    from app import email_service
    from app.utils.graph_client import GraphClient

    excess_list = _guard_owner(db, list_id, owner)
    line_ids = _target_line_ids(db, excess_list, scope, line_item_ids)

    # Resolve every buyer up front (card + send address) so the payload and the row
    # writing share one canonical "who" per buyer.
    resolved: list[tuple[dict, VendorCard, str | None]] = []
    vendor_groups: list[dict] = []
    for buyer in buyers:
        card = _resolve_buyer_card(db, buyer)
        email = buyer.get("email") or _primary_email(card)
        resolved.append((buyer, card, email))
        vendor_groups.append(
            {
                "vendor_name": card.display_name,
                "vendor_email": email or "",
                "parts": [p["part_number"] for line_id in line_ids for p in _parts_snapshot(db, excess_list, line_id)],
                "subject": subject,
                "body": body,
            }
        )

    # Reuse the RFQ send engine in its no-requisition mode (email out, no Contact rows).
    send_results = await email_service.send_batch_rfq(
        token=token,
        db=db,
        user_id=owner.id,
        requisition_id=None,
        vendor_groups=vendor_groups,
    )
    # Index results by recipient email (send_batch_rfq preserves vendor identity in each
    # result dict) so we can map a per-buyer outcome back to its rows.
    result_by_email: dict[str, dict] = {(r.get("vendor_email") or "").lower(): r for r in send_results}

    gc = GraphClient(token)
    send_time = datetime.now(timezone.utc)
    all_rows: list[ExcessOutreach] = []
    for _buyer, card, email in resolved:
        result = result_by_email.get((email or "").lower(), {})
        sent_ok = result.get("status") == "sent"
        status = ExcessOutreachStatus.SENT if sent_ok else ExcessOutreachStatus.NO_RESPONSE

        rows = _make_outreach_rows(
            db,
            excess_list=excess_list,
            owner=owner,
            card=card,
            channel=ExcessOutreachChannel.EMAIL,
            line_ids=line_ids,
            status=status,
            sent_at=send_time if sent_ok else None,
        )

        # Stamp graph ids on the SENT buyer's rows via the same source-level lookup
        # send_batch_rfq uses internally (we cannot get them from the result dict).
        if sent_ok and email:
            try:
                sent_msg = await email_service._find_sent_message(gc, subject, email)
            except Exception:  # pragma: no cover - lookup is best-effort
                logger.warning("Outreach sent-message lookup failed for <{}>", email, exc_info=True)
                sent_msg = None
            if isinstance(sent_msg, dict) and sent_msg:
                for row in rows:
                    row.graph_message_id = sent_msg.get("id")
                    row.graph_conversation_id = sent_msg.get("conversationId")
            else:
                logger.warning(
                    "Outreach graph ids left NULL for buyer '{}' <{}> — reply matching degrades",
                    card.display_name,
                    email,
                )

        all_rows.extend(rows)
        _log_outreach_activity(
            db,
            owner=owner,
            excess_list=excess_list,
            card=card,
            channel=ExcessOutreachChannel.EMAIL,
            sent=sent_ok,
        )

    db.commit()
    for row in all_rows:
        db.refresh(row)
    logger.info(
        "Sent {} outreach email row(s) on list={} ({} buyer(s)) by owner={}",
        len(all_rows),
        list_id,
        len(buyers),
        owner.id,
    )
    return all_rows


def _primary_email(card: VendorCard) -> str | None:
    """First usable email on a vendor card (the buyer's send address), or None."""
    for e in card.emails or []:
        if e and "@" in e:
            return e.strip()
    return None


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
    from .excess_service import recompute_line_rollup

    for line_item_id in affected:
        recompute_line_rollup(db, line_item_id)

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

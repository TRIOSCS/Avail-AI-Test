"""bid_back_service.py — Assemble + clean-export the outbound bid back (Chunk E).

The owner assembles selected inbound offers into ONE customer-facing CustomerBid — the
offer Trio sends BACK to the stock holder to buy their excess. Each line is priced from
the best-per-unit rollup (``ExcessLineItem.best_offer_unit_price``), overridable per line;
the offer that informed the price is recorded INTERNALLY for audit but is NEVER exported.

Cleanliness is enforced HERE, at assembly / export, not by template omission alone
(spec §"Customer Bid"): ``bid_back_export_context`` is a pure whitelist — its line dicts
carry ONLY part / mfr / qty / condition / our unit + extended price, and the header
carries no seller-company identity. The Quote PDF accidentally hid the vendor by never
passing the field; the bid back makes that guarantee explicit and testable.

Called by: routers.resell (Build Bid tab), services.document_service (generate_bid_report_pdf).
Depends on: models.excess (CustomerBid/Line, ExcessList, ExcessLineItem), constants,
    a request-scoped Session.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..constants import CustomerBidStatus
from ..models import User
from ..models.excess import (
    CustomerBid,
    CustomerBidLine,
    ExcessLineItem,
    ExcessList,
    ExcessOffer,
    ExcessOfferLine,
)


def _safe_commit(db: Session, *, entity: str = "record") -> None:
    """Commit the session, mapping IntegrityError to HTTP 409 instead of an unhandled
    500.

    A dangling provenance pointer (e.g. a corrupt ``best_offer_id``) is sanitized before it
    reaches a real FK column, so this is a backstop: any OTHER conflicting write surfaces as
    a clean 409 rather than a 500 out of the global handler.
    """
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        logger.warning("IntegrityError on {}: {}", entity, exc)
        raise HTTPException(409, f"Duplicate or conflicting {entity}") from exc


def build_bid_back(
    db: Session,
    *,
    list_id: int,
    owner: User,
    selections: list[dict],
) -> CustomerBid:
    """Assemble a draft CustomerBid from selected lines of *list_id* — owner-only.

    For each selection (a dict with ``excess_line_item_id`` and optional
    ``customer_unit_price`` / ``selected_offer_id`` / ``selected_offer_line_id`` /
    ``quantity``), create a CustomerBidLine whose ``customer_unit_price`` defaults to the
    line's ``best_offer_unit_price`` rollup and is overridden by an explicit selection
    price. The selected-offer ids are recorded for internal audit only — they are never
    exported, and any that no longer resolve to a live offer/offer-line on this list are
    dropped to NULL (a stale ``best_offer_id`` can never 500 the assembly). The line
    quantity defaults to the posted line's quantity.

    Re-assemble semantics (M4): the list keeps ONE CustomerBid row across revisions.
    When a bid already exists, re-assembling BUMPS ``revision`` on the SAME row and
    replaces its lines (preserving the audit chain) instead of orphaning a fresh draft.
    A new revision resets the row to ``draft`` and clears the prior send/response
    stamps — they belonged to the superseded revision.

    Guards (raise HTTPException, never silent): the list must exist (404); *owner* must
    own the list (403 — assembling a bid back is the owner's privilege); and every
    selected line must belong to *list_id* (404 — never price a foreign line). Returns
    the persisted draft CustomerBid with its lines loaded.
    """
    from .excess_service import get_excess_list

    excess_list = get_excess_list(db, list_id)
    if excess_list.owner_id != owner.id:
        raise HTTPException(403, "Only the list owner can build the bid back")

    # Index the posting's lines so we can validate each selection belongs here and seed
    # the price from the rollup.
    posted: dict[int, ExcessLineItem] = {
        it.id: it for it in db.query(ExcessLineItem).filter_by(excess_list_id=list_id).all()
    }

    # The set of REAL provenance ids for this list. ``selected_offer_id`` /
    # ``selected_offer_line_id`` are hard FKs, but ``ExcessLineItem.best_offer_id`` is a
    # plain int (no FK) that can go stale — a corrupt rollup once held an offer-LINE id
    # instead of an ExcessOffer id, and writing that dangling value into the FK column
    # raised IntegrityError → an unhandled 500. Resolve every provenance pointer against
    # these sets and drop anything that no longer points at a live row on THIS list.
    valid_offer_ids = set(db.scalars(select(ExcessOffer.id).where(ExcessOffer.excess_list_id == list_id)).all())
    valid_offer_line_ids = set(
        db.scalars(
            select(ExcessOfferLine.id)
            .join(ExcessOffer, ExcessOfferLine.offer_id == ExcessOffer.id)
            .where(ExcessOffer.excess_list_id == list_id)
        ).all()
    )

    # One bid per list: re-assemble bumps ``revision`` on the same row (audit chain
    # preserved) instead of leaving a pile of orphan drafts.
    bid = db.query(CustomerBid).filter(CustomerBid.excess_list_id == list_id).order_by(CustomerBid.id.desc()).first()
    if bid is None:
        bid = CustomerBid(
            excess_list_id=list_id,
            owner_id=owner.id,
            status=CustomerBidStatus.DRAFT,
            notes=None,
        )
        db.add(bid)
        db.flush()  # need bid.id before attaching lines
    else:
        bid.revision = (bid.revision or 1) + 1
        bid.status = CustomerBidStatus.DRAFT
        bid.sent_at = None
        bid.responded_at = None
        bid.responded_by_id = None
        for existing_line in list(bid.lines):
            db.delete(existing_line)
        db.flush()  # clear the prior revision's lines before attaching the new ones

    for sel in selections or []:
        line_item_id = sel.get("excess_line_item_id")
        item = posted.get(line_item_id) if line_item_id is not None else None
        if item is None:
            raise HTTPException(404, f"Line item {line_item_id} is not part of list {list_id}")

        override = sel.get("customer_unit_price")
        unit_price = _to_decimal(override) if override is not None else item.best_offer_unit_price
        quantity = sel.get("quantity") or item.quantity

        # Resolve provenance against the live rows — a dangling id is dropped (NULL),
        # never written into the FK column where it would 500 on commit.
        candidate_offer_id = sel.get("selected_offer_id") or item.best_offer_id
        selected_offer_id = candidate_offer_id if candidate_offer_id in valid_offer_ids else None
        candidate_offer_line_id = sel.get("selected_offer_line_id")
        selected_offer_line_id = candidate_offer_line_id if candidate_offer_line_id in valid_offer_line_ids else None

        db.add(
            CustomerBidLine(
                customer_bid_id=bid.id,
                excess_line_item_id=item.id,
                selected_offer_id=selected_offer_id,
                selected_offer_line_id=selected_offer_line_id,
                customer_unit_price=unit_price,
                quantity=quantity,
            )
        )

    _safe_commit(db, entity="customer bid")
    db.refresh(bid)
    logger.info(
        "Assembled CustomerBid id={} rev={} on list={} by owner={} ({} lines)",
        bid.id,
        bid.revision,
        list_id,
        owner.id,
        len(bid.lines),
    )
    return bid


# ---------------------------------------------------------------------------
# Lifecycle transitions: draft -> sent -> accepted/rejected (M4)
# ---------------------------------------------------------------------------


def _bid_number(bid: CustomerBid) -> str:
    """The customer-facing bid number (matches ``bid_back_export_context``)."""
    return f"BID-{bid.id}"


def _guard_bid_for_owner(db: Session, *, list_id: int, bid_id: int, owner: User) -> tuple[ExcessList, CustomerBid]:
    """Load a bid + its list, enforcing owner-only + belongs-to-list (raise, never
    silent).

    404 when the list or bid is missing, or the bid belongs to another list (existence
    not revealed across lists); 403 when *owner* does not own the list. Shared by every
    lifecycle transition so send / accept / reject enforce one guard.
    """
    from .excess_service import get_excess_list

    excess_list = get_excess_list(db, list_id)
    if excess_list.owner_id != owner.id:
        raise HTTPException(403, "Only the list owner can act on the bid back")
    bid = db.get(CustomerBid, bid_id)
    if bid is None or bid.excess_list_id != list_id:
        raise HTTPException(404, f"Bid {bid_id} not found on list {list_id}")
    return excess_list, bid


def _site_contact(site) -> tuple[str | None, str | None]:
    """(name, email) for a CustomerSite — its own contact, else its primary SiteContact.

    Prefers the site-level ``contact_email`` (one per site); falls back to the primary
    active SiteContact (or the first contactable one). Skips ``do_not_contact`` contacts.
    Returns ``(None, None)`` when nothing is reachable.
    """
    if site is None:
        return None, None
    if site.contact_email:
        return site.contact_name, site.contact_email
    contacts = [c for c in site.site_contacts if c.email and not c.do_not_contact]
    primary = next((c for c in contacts if c.is_primary), None) or (contacts[0] if contacts else None)
    if primary:
        return primary.full_name, primary.email
    return None, None


def resolve_seller_contact(db: Session, excess_list: ExcessList) -> tuple[str | None, str | None]:
    """Resolve the seller's send contact ``(name, email)`` for a bid back.

    The bid back goes to the CUSTOMER (the stock holder Trio is buying from), so the
    recipient is the seller's own contact — NOT anonymized (identity hiding shields the
    seller from OFFERERS, never from the seller themselves). Resolution order: the list's
    own ``customer_site`` (its contact / primary SiteContact), then any active site on the
    seller company (primary-first). Returns ``(None, None)`` when no email is on file — the
    caller must refuse to send rather than email nobody.
    """
    from ..models.crm import CustomerSite

    site = excess_list.customer_site
    if site is None and excess_list.customer_site_id:
        site = db.get(CustomerSite, excess_list.customer_site_id)
    name, email = _site_contact(site)
    if email:
        return name, email

    for candidate in (
        db.query(CustomerSite)
        .filter(CustomerSite.company_id == excess_list.company_id, CustomerSite.is_active.is_(True))
        .order_by(CustomerSite.id)
        .all()
    ):
        name, email = _site_contact(candidate)
        if email:
            return name, email
    return None, None


def _default_bid_email(bid: CustomerBid, contact_name: str | None) -> tuple[str, str]:
    """Default cover-note subject + body for the bid-back send (identity-safe to the
    seller)."""
    greeting = f"Hi {contact_name}," if contact_name else "Hello,"
    number = _bid_number(bid)
    subject = f"Our offer for your excess inventory — {number}"
    body = (
        f"{greeting}\n\n"
        "Thank you for sharing your excess inventory with us. Please find our offer "
        f"attached ({number}, revision {bid.revision or 1}). The attached PDF lists each "
        "part, quantity, condition, and our unit price.\n\n"
        "We look forward to your response.\n\n"
        "Best regards,\n"
        "Trio Supply Chain Solutions"
    )
    return subject, body


async def _bid_pdf_attachment(db: Session, bid: CustomerBid):
    """Render the clean bid-back PDF and wrap it as a Graph sendMail attachment.

    Runs the (sync, CPU-bound) WeasyPrint render in a thread executor — the same pattern
    the download endpoint uses — so it never blocks the event loop.
    """
    import asyncio
    import base64

    from .document_service import generate_bid_report_pdf
    from .rfq_attachments import RfqAttachment

    loop = asyncio.get_running_loop()
    pdf_bytes = await loop.run_in_executor(None, generate_bid_report_pdf, bid.id, db)
    return RfqAttachment(
        name=f"bid-{bid.id}.pdf",
        content_type="application/pdf",
        content_bytes_b64=base64.b64encode(pdf_bytes).decode("ascii"),
    )


async def send_bid_back(
    db: Session,
    *,
    list_id: int,
    bid_id: int,
    owner: User,
    token: str,
    subject: str | None = None,
    body: str | None = None,
) -> CustomerBid:
    """Email the clean bid-back PDF to the seller and flip the bid ``draft -> sent``.

    Owner-only (via :func:`_guard_bid_for_owner`). Guards: the bid must be a ``draft``
    (409 otherwise — a sent/decided bid is not re-sendable; re-assemble first to bump the
    revision) and carry at least one line (409). The seller contact email must resolve
    (422 otherwise — never email nobody). Reuses ``email_service.send_batch_rfq`` in its
    no-requisition mode (DNC-at-send / save-to-sent / retry for free) with the clean PDF
    as the sole attachment; the PDF is the whitelisted bid_back_export_context, so no
    broker / trader / source identity crosses into it. Only on a confirmed ``sent`` result
    does the status flip and ``sent_at`` stamp — a failed send raises 502 and leaves the
    bid a draft. Commits. Returns the refreshed bid.
    """
    excess_list, bid = _guard_bid_for_owner(db, list_id=list_id, bid_id=bid_id, owner=owner)
    if bid.status != CustomerBidStatus.DRAFT:
        raise HTTPException(409, "Only a draft bid can be sent — re-assemble to revise a sent bid")
    if not bid.lines:
        raise HTTPException(409, "Add at least one line before sending the bid")

    contact_name, contact_email = resolve_seller_contact(db, excess_list)
    if not contact_email:
        raise HTTPException(422, "No customer contact email on file to send this bid to")

    attachment = await _bid_pdf_attachment(db, bid)
    if not subject or not body:
        default_subject, default_body = _default_bid_email(bid, contact_name)
        subject = subject or default_subject
        body = body or default_body

    from app import email_service

    results = await email_service.send_batch_rfq(
        token=token,
        db=db,
        user_id=owner.id,
        requisition_id=None,
        vendor_groups=[
            {
                "vendor_name": contact_name or "Customer",
                "vendor_email": contact_email,
                "parts": [],
                "subject": subject,
                "body": body,
            }
        ],
        attachments=[attachment],
    )
    result = next(
        (r for r in results if (r.get("vendor_email") or "").lower() == contact_email.lower()),
        results[0] if results else {},
    )
    if result.get("status") != "sent":
        reason = result.get("error") or result.get("status") or "unknown error"
        raise HTTPException(502, f"Bid email could not be sent ({reason})")

    bid.status = CustomerBidStatus.SENT
    bid.sent_at = datetime.now(UTC)
    db.commit()
    db.refresh(bid)
    logger.info(
        "Sent CustomerBid id={} rev={} to <{}> on list={} by owner={}",
        bid.id,
        bid.revision,
        contact_email,
        list_id,
        owner.id,
    )
    return bid


def record_bid_response(
    db: Session,
    *,
    list_id: int,
    bid_id: int,
    owner: User,
    accepted: bool,
) -> CustomerBid:
    """Record the seller's answer on a sent bid — ``sent -> accepted`` or ``sent ->
    rejected``.

    Owner-only (the trader logs the seller's verbal/written reply — the seller is not a
    User). Guards: the bid must be ``sent`` (409 otherwise — you cannot accept a draft or
    re-decide a terminal bid). Stamps ``responded_at`` + ``responded_by_id`` (who/when).
    Commits. Returns the refreshed bid.
    """
    _excess_list, bid = _guard_bid_for_owner(db, list_id=list_id, bid_id=bid_id, owner=owner)
    if bid.status != CustomerBidStatus.SENT:
        raise HTTPException(409, "Only a sent bid can be accepted or rejected")

    bid.status = CustomerBidStatus.ACCEPTED if accepted else CustomerBidStatus.REJECTED
    bid.responded_at = datetime.now(UTC)
    bid.responded_by_id = owner.id
    db.commit()
    db.refresh(bid)
    logger.info("Recorded CustomerBid id={} response={} by owner={}", bid.id, bid.status, owner.id)
    return bid


def bid_back_export_context(bid: CustomerBid) -> dict:
    """Build the CLEAN customer-facing export payload for *bid* (pure whitelist).

    The returned dict's ``line_items`` carry ONLY ``part_number`` / ``manufacturer`` /
    ``quantity`` / ``condition`` / ``unit_price`` / ``extended_price`` — every
    trader / vendor / offerer / source field is STRIPPED here (the CustomerBidLine's
    ``selected_offer_id`` / ``selected_offer_line_id`` and the ExcessLineItem's
    ``best_offer_*`` rollup never cross into the export). The header carries the bid
    number, revision, generated date and totals — NEVER the seller company name (the
    customer doc is identity-clean; spec §"Customer identity hiding"). This is the single
    source the PDF template renders from, so cleanliness is guaranteed at assembly, not
    by hoping the template omits a field.
    """
    line_items: list[dict] = []
    subtotal = 0.0
    for ln in sorted(bid.lines, key=lambda x: x.id):
        item = ln.excess_line_item
        unit = float(ln.customer_unit_price) if ln.customer_unit_price is not None else None
        qty = ln.quantity or 0
        extended = (unit * qty) if unit is not None else None
        if extended is not None:
            subtotal += extended
        # WHITELIST — explicitly enumerate the clean fields. No part of the inbound
        # offer / rollup is referenced, so nothing leaky can ride along.
        line_items.append(
            {
                "part_number": (item.part_number if item else None),
                "manufacturer": (item.manufacturer if item else None),
                "quantity": qty,
                "condition": (item.condition if item else None),
                "unit_price": unit,
                "extended_price": extended,
            }
        )

    return {
        "bid_number": f"BID-{bid.id}",
        "revision": bid.revision or 1,
        "status": bid.status,
        "notes": bid.notes,
        "line_items": line_items,
        "subtotal": round(subtotal, 2),
        "line_count": len(line_items),
    }


def _to_decimal(value) -> Decimal | None:
    """Coerce an override price to Decimal, or None when blank/invalid."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip().lstrip("$").replace(",", ""))
    except (ArithmeticError, ValueError):
        return None

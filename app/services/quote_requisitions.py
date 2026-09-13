# app/services/quote_requisitions.py
"""services/quote_requisitions.py — the single arbitration point for the quote ⇄
requisition join (``quote_requisitions``).

A combined quote (OQ-02/REQ-04) spans line items from 2+ requisitions selected together
in the list "Build Quote" flow. ``Quote.requisition_id`` still records the PRIMARY/anchor
requisition; the join table links a quote to EVERY contributing requisition so a SECONDARY
requisition's surfaces (list Quotes column, quotes tab, offers-tab draft lookup, quote
detail) also see the combined quote — instead of going blind because the old
``Quote.requisition_id == req_id`` filter only matched the anchor.

Invariant: every quote has ≥1 join row (its primary self-row). Existing quotes were
backfilled by migration 175; every NEW quote gets its self-row automatically via the
``Quote`` ``after_insert`` listener in ``app/models/quotes.py`` — so quotes created by ANY
path (builder, revise, proactive, offers, CRM) are visible on their requisition. This
module adds the ADDITIONAL contributing-requisition rows for combined quotes
(``link_quote_to_requisitions``) and owns every requisition-scoped read.

Called by: routers/quote_builder.py, services/quote_builder_service.py,
    routers/htmx/quotes.py, routers/htmx/requisitions.py, services/quote_send.py,
    services/buyplan_builder.py.
Depends on: app.models (Quote, QuoteRequisition, Requisition).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy.orm import Query, Session

if TYPE_CHECKING:
    from app.models import Quote, User


class CustomerMismatchError(ValueError):
    """A set of requisitions cannot share one combined quote (different/absent
    customer).

    Carries ``.detail`` — a customer-safe, honest message that names each offending
    requisition and its resolved customer — so the router can surface it verbatim as an
    HTTP 400 (global htmx toast / builder save banner) instead of a silent drop.
    """

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def _customer_name_for_site(db: Session, customer_site_id: int | None) -> str:
    """Resolve the customer company name for a requisition's customer site ("" if none).

    Lives in this service (not the router) so the layering runs router → service; the
    quote_builder router imports it from here.
    """
    if not customer_site_id:
        return ""
    from app.models import CustomerSite

    site = db.get(CustomerSite, customer_site_id)
    if site and site.company:
        return site.company.name or ""
    return ""


def validate_same_customer(db: Session, req_ids: list[int]) -> int:
    """Ensure every requisition in *req_ids* shares one non-null ``customer_site_id``.

    Returns that shared ``customer_site_id`` on success. Raises ``CustomerMismatchError``
    (mapped to HTTP 400 by the caller) when any requisition has no customer site linked,
    or when the selected requisitions belong to different customer sites — the two ways a
    combined quote would silently misattribute lines. The message names each offending
    requisition and its resolved customer so the salesperson can fix the selection.
    """
    from app.models import Requisition

    if not req_ids:
        raise CustomerMismatchError("No requisitions selected.")

    reqs = {r.id: r for r in db.query(Requisition).filter(Requisition.id.in_(req_ids)).all()}

    def _label(rid: int) -> str:
        r = reqs.get(rid)
        return f"{r.name} (#{rid})" if r and r.name else f"#{rid}"

    # (1) Every requisition must have a customer site — otherwise there is no customer to
    # attribute the combined quote's lines to.
    missing = [rid for rid in req_ids if not (reqs.get(rid) and reqs[rid].customer_site_id)]
    if missing:
        names = ", ".join(_label(rid) for rid in missing)
        raise CustomerMismatchError(
            f"Every requisition needs a linked customer before a combined quote can be "
            f"built. Missing a customer: {names}."
        )

    # (2) All customer sites must be identical — sites differ in contact/terms, so even the
    # same company across two sites is a genuine mismatch we must not paper over.
    site_ids = {reqs[rid].customer_site_id for rid in req_ids}
    if len(site_ids) > 1:
        breakdown = "; ".join(
            f"{_label(rid)} → {_customer_name_for_site(db, reqs[rid].customer_site_id) or 'customer site #' + str(reqs[rid].customer_site_id)}"
            for rid in req_ids
        )
        raise CustomerMismatchError(
            f"A combined quote must be for one customer, but the selected requisitions "
            f"belong to different customers: {breakdown}."
        )

    return site_ids.pop()


def link_quote_to_requisitions(db: Session, quote_id: int, req_ids: list[int]) -> None:
    """Idempotently link *quote_id* to every requisition in *req_ids* (order preserved).

    The primary self-row is already present (created by the ``Quote`` ``after_insert``
    listener), so this typically adds only the non-primary contributing requisitions of a
    combined quote. Re-runnable: existing links are skipped, so a revise/re-save never
    duplicates a row (the ``uq_quote_requisition`` unique constraint is the backstop).
    """
    from app.models import QuoteRequisition

    existing = {
        rid for (rid,) in db.query(QuoteRequisition.requisition_id).filter(QuoteRequisition.quote_id == quote_id).all()
    }
    for rid in req_ids:
        if rid in existing:
            continue
        db.add(QuoteRequisition(quote_id=quote_id, requisition_id=rid))
        existing.add(rid)
    db.flush()


def requisition_ids_for_quote(db: Session, quote_id: int) -> list[int]:
    """Every contributing requisition id for *quote_id*, primary first.

    Ordered by ``QuoteRequisition.id`` (insertion order): the primary self-row is written
    first, so ``[0]`` is the anchor — matching ``Quote.requisition_id``.
    """
    from app.models import QuoteRequisition

    rows = (
        db.query(QuoteRequisition.requisition_id)
        .filter(QuoteRequisition.quote_id == quote_id)
        .order_by(QuoteRequisition.id.asc())
        .all()
    )
    return [rid for (rid,) in rows]


def requisitions_for_quote(db: Session, quote_id: int) -> list:
    """Hydrated ``Requisition`` rows contributing to *quote_id*, primary first.

    Same ordering as ``requisition_ids_for_quote`` — used by the quote detail template to
    list each contributing requisition as a link.
    """
    from app.models import QuoteRequisition, Requisition

    return (
        db.query(Requisition)
        .join(QuoteRequisition, QuoteRequisition.requisition_id == Requisition.id)
        .filter(QuoteRequisition.quote_id == quote_id)
        .order_by(QuoteRequisition.id.asc())
        .all()
    )


def quotes_for_requisition(db: Session, req_id: int) -> Query:
    """A ``Query[Quote]`` of every quote contributing to requisition *req_id* (join-
    based).

    Replaces the old ``Quote.requisition_id == req_id`` read filter so a SECONDARY
    requisition surfaces the combined quotes it contributes to, not just the ones it
    anchors. Returns a ``Query`` so callers add their own ``.filter``/``.order_by``/
    ``.first``/``.all`` (each quote links a given requisition at most once, so the join
    never duplicates a quote row for a single ``req_id``).
    """
    from app.models import Quote, QuoteRequisition

    return (
        db.query(Quote)
        .join(QuoteRequisition, QuoteRequisition.quote_id == Quote.id)
        .filter(QuoteRequisition.requisition_id == req_id)
    )


def apply_quote_result(db: Session, quote, *, result: str, reason: str | None = None, notes: str | None = None) -> None:
    """Record a won/lost outcome on a quote AND every contributing requisition.

    The SINGLE source of truth for both quote-result routes (the HTMX Approvals
    workspace and the JSON API). They had diverged: the workspace path set only the
    quote's status, leaving the requisition open and won_revenue unset — so the owner's
    win metrics never recorded the win (process-review D5). This also transitions EVERY
    contributing requisition (combined quotes span several), never clobbering one
    already WON/LOST, and writes the outcome ActivityLog the JSON twin did.

    Caller commits.
    """
    from datetime import UTC, datetime

    from app.constants import QuoteStatus, RequisitionStatus
    from app.models import ActivityLog, CustomerSite, Requisition
    from app.services.status_machine import require_valid_transition

    if result not in (QuoteStatus.WON, QuoteStatus.LOST):
        raise ValueError("Result must be 'won' or 'lost'")

    require_valid_transition("quote", quote.status, result)
    quote.result = result
    quote.result_reason = reason
    quote.result_notes = notes
    quote.result_at = datetime.now(UTC)
    quote.status = result
    if result == QuoteStatus.WON:
        quote.won_revenue = quote.subtotal

    # Every contributing requisition — combined quotes span 2+. Never clobber one
    # already terminal.
    for rid in requisition_ids_for_quote(db, quote.id) or [quote.requisition_id]:
        req = db.get(Requisition, rid)
        if req and req.status not in (RequisitionStatus.WON, RequisitionStatus.LOST):
            req.status = result

    # Outcome notification on the PRIMARY requisition's creator (one row).
    primary = db.get(Requisition, quote.requisition_id)
    if primary and primary.created_by:
        customer = primary.customer_name or primary.name or ""
        if result == QuoteStatus.WON:
            subj = f"Quote won: {customer} — ${quote.subtotal or 0:,.0f}"
        else:
            subj = f"Quote lost: {customer} — {reason or 'no reason'}"
        company_id = None
        if primary.customer_site_id:
            site = db.get(CustomerSite, primary.customer_site_id)
            company_id = site.company_id if site else None
        db.add(
            ActivityLog(
                user_id=primary.created_by,
                activity_type=f"quote_{result}",
                channel="system",
                requisition_id=primary.id,
                quote_id=quote.id,
                contact_name=customer,
                subject=subj,
                company_id=company_id,
            )
        )
        if company_id:
            from app.services.activity_service import _update_last_activity

            _update_last_activity({"type": "company", "id": company_id}, db)


def reopen_quote(db: Session, quote: Quote, actor: User, *, revise: bool) -> None:
    """Undo a quote's terminal outcome on its requisition when the quote is reopened.

    The single arbitration point for both reopen routes (JSON ``/api/quotes/{id}/reopen``
    and the HTMX ``/v2/partials/quotes/{id}/reopen``) — they had diverged: the JSON route
    set ``req.status = OPEN`` directly (bypassing ``transition()``, so no ActivityLog and
    a stale ``won_revenue``), while the HTMX route never touched the requisition at all,
    leaving it WON/LOST after the quote went back to draft.

    Transitions the PRIMARY requisition back to OPEN via
    ``requisition_state.transition`` (a no-op if it's already open; any other illegal
    origin is logged and ignored rather than raised, since reopening the quote should
    never itself fail). ``transition()`` already clears a stale ``outcome_reason`` for
    non-terminal transitions. Also nulls ``quote.won_revenue`` since a reopened quote is
    no longer a recorded win. Caller commits.

    ``revise`` only affects the log message (a revision keeps the reopened quote's
    lineage distinct from a plain undo-and-resend) — the requisition/quote mutation is
    identical either way; the revision itself is built separately by
    ``build_quote_revision``.
    """
    from app.constants import RequisitionStatus
    from app.models import Requisition
    from app.services.requisition_state import transition

    quote.won_revenue = None

    req = db.get(Requisition, quote.requisition_id)
    if not req:
        return
    try:
        transition(req, RequisitionStatus.OPEN, actor, db)
    except ValueError as e:
        logger.info("Requisition {} not reopened to open (quote {} reopen): {}", req.id, quote.id, e)
        return
    action = "revision" if revise else "reopen"
    logger.info("Quote {} {} — requisition {} reset to open", quote.id, action, req.id)


def build_quote_revision(db: Session, old: Quote, actor: User) -> Quote:
    """Build the next revision of *old*: marks *old* REVISED and returns a new, flushed
    (uncommitted) Quote carrying its line items, terms, and requisition membership
    forward.

    Numbering convention (oq-04, unified 2026-08-17): the superseded quote KEEPS
    its number; the new revision carries the -R trail (Q-0142 → Q-0142-R1 → -R2).

    Clones the parent's ``QuoteLine`` rows (not just the ``line_items`` JSON) —
    quote_detail_partial, the send email, the PDF, and Build-Buy-Plan all read
    QuoteLine — and calls ``link_quote_to_requisitions`` so a combined quote's
    revision stays visible on every contributing requisition (the ``Quote``
    ``after_insert`` listener only adds the new quote's own primary self-row).
    Raises ``HTTPException(409)`` (via ``require_valid_transition``) if REVISED is
    not a legal transition from ``old.status``. Caller commits.
    """
    from app.constants import QuoteStatus
    from app.models import Quote, QuoteLine
    from app.services.crm_service import quote_base_number, revision_quote_number
    from app.services.status_machine import require_valid_transition

    require_valid_transition("quote", old.status, QuoteStatus.REVISED)
    old.status = QuoteStatus.REVISED
    new_revision = (old.revision or 1) + 1
    new_quote = Quote(
        requisition_id=old.requisition_id,
        customer_site_id=old.customer_site_id,
        quote_number=revision_quote_number(quote_base_number(old.quote_number), new_revision),
        revision=new_revision,
        line_items=old.line_items or [],
        subtotal=old.subtotal,
        total_cost=old.total_cost,
        total_margin_pct=old.total_margin_pct,
        payment_terms=old.payment_terms,
        shipping_terms=old.shipping_terms,
        validity_days=old.validity_days,
        notes=old.notes,
        status=QuoteStatus.DRAFT,
        created_by_id=actor.id,
        source=old.source,
    )
    db.add(new_quote)
    db.flush()  # need new_quote.id for the cloned lines + link table

    link_quote_to_requisitions(db, new_quote.id, requisition_ids_for_quote(db, old.id))

    for src in db.query(QuoteLine).filter(QuoteLine.quote_id == old.id).all():
        db.add(
            QuoteLine(
                quote_id=new_quote.id,
                material_card_id=src.material_card_id,
                offer_id=src.offer_id,
                mpn=src.mpn,
                description=src.description,
                manufacturer=src.manufacturer,
                qty=src.qty,
                cost_price=src.cost_price,
                sell_price=src.sell_price,
                margin_pct=src.margin_pct,
                currency=src.currency,
            )
        )
    return new_quote

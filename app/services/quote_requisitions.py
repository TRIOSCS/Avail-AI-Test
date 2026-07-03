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

from sqlalchemy.orm import Query, Session


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

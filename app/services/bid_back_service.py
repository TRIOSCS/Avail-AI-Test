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

Called by: routers.trading (Build Bid tab), services.document_service (generate_bid_report_pdf).
Depends on: models.excess (CustomerBid/Line, ExcessList, ExcessLineItem), constants,
    a request-scoped Session.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.orm import Session

from ..constants import CustomerBidStatus
from ..models import User
from ..models.excess import CustomerBid, CustomerBidLine, ExcessLineItem


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
    exported. The line quantity defaults to the posted line's quantity.

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

    bid = CustomerBid(
        excess_list_id=list_id,
        owner_id=owner.id,
        status=CustomerBidStatus.DRAFT,
        notes=None,
    )
    db.add(bid)
    db.flush()  # need bid.id before attaching lines

    for sel in selections or []:
        line_item_id = sel.get("excess_line_item_id")
        item = posted.get(line_item_id) if line_item_id is not None else None
        if item is None:
            raise HTTPException(404, f"Line item {line_item_id} is not part of list {list_id}")

        override = sel.get("customer_unit_price")
        unit_price = _to_decimal(override) if override is not None else item.best_offer_unit_price
        quantity = sel.get("quantity") or item.quantity

        db.add(
            CustomerBidLine(
                customer_bid_id=bid.id,
                excess_line_item_id=item.id,
                selected_offer_id=sel.get("selected_offer_id") or item.best_offer_id,
                selected_offer_line_id=sel.get("selected_offer_line_id"),
                customer_unit_price=unit_price,
                quantity=quantity,
            )
        )

    db.commit()
    db.refresh(bid)
    logger.info(
        "Assembled CustomerBid id={} on list={} by owner={} ({} lines)",
        bid.id,
        list_id,
        owner.id,
        len(bid.lines),
    )
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

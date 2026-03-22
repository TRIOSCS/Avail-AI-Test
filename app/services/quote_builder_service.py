# app/services/quote_builder_service.py
"""services/quote_builder_service.py — Business logic for the Quote Builder.

Loads requirement + offer data for the builder modal, applies smart defaults,
generates Excel exports. Decoupled from HTTP.

Called by: app.routers.quote_builder
Depends on: app.models (Requirement, Offer, Quote), openpyxl
"""

from __future__ import annotations

from sqlalchemy.orm import Session, joinedload


def get_builder_data(
    req_id: int,
    db: Session,
    requirement_ids: list[int] | None = None,
) -> list[dict]:
    """Load requirements + offers for the quote builder modal."""
    from app.models import Requirement

    query = db.query(Requirement).options(joinedload(Requirement.offers)).filter(Requirement.requisition_id == req_id)
    if requirement_ids:
        query = query.filter(Requirement.id.in_(requirement_ids))
    query = query.order_by(Requirement.id)
    requirements = query.all()

    lines = []
    for r in requirements:
        from app.constants import OfferStatus

        active_offers = [o for o in r.offers if o.status == OfferStatus.ACTIVE]
        offers_data = []
        for o in active_offers:
            offers_data.append(
                {
                    "id": o.id,
                    "vendor_name": o.vendor_name,
                    "unit_price": float(o.unit_price) if o.unit_price else 0,
                    "qty_available": o.qty_available or 0,
                    "lead_time": o.lead_time,
                    "date_code": o.date_code,
                    "condition": o.condition,
                    "packaging": o.packaging,
                    "moq": o.moq,
                    "confidence": o.parse_confidence,
                    "notes": o.notes,
                }
            )

        lines.append(
            {
                "requirement_id": r.id,
                "mpn": r.primary_mpn,
                "manufacturer": r.manufacturer,
                "target_qty": r.target_qty or 0,
                "target_price": float(r.target_price) if r.target_price else None,
                "customer_pn": r.customer_pn,
                "date_codes": r.date_codes,
                "condition": r.condition,
                "packaging": r.packaging,
                "firmware": r.firmware,
                "hardware_codes": r.hardware_codes,
                "sale_notes": r.sale_notes,
                "need_by_date": str(r.need_by_date) if r.need_by_date else None,
                "offers": offers_data,
                "offer_count": len(offers_data),
                "status": "unknown",
                "selected_offer_id": None,
                "sell_price": None,
                "sell_price_manual": False,
                "buyer_notes": "",
                "pricing_history": None,
            }
        )

    # Load pricing history for all MPNs in one pass
    try:
        from app.routers.crm._helpers import _preload_last_quoted_prices

        quoted_prices = _preload_last_quoted_prices(db)
        for line in lines:
            mpn_key = (line["mpn"] or "").upper().strip()
            lq = quoted_prices.get(mpn_key)
            if lq:
                line["pricing_history"] = {
                    "avg_price": lq.get("sell_price"),
                    "price_range": None,
                    "recent": [
                        {
                            "quote_number": lq.get("quote_number", ""),
                            "date": lq.get("date", ""),
                            "cost": lq.get("cost_price", lq.get("sell_price")),
                            "sell": lq.get("sell_price"),
                            "margin": lq.get("margin_pct"),
                            "result": lq.get("result"),
                        }
                    ],
                }
    except Exception:
        pass  # Pricing history is non-critical; degrade gracefully

    return lines


def apply_smart_defaults(lines: list[dict]) -> None:
    """Apply smart defaults to builder lines in-place.

    - 1 offer: auto-select, status = decided
    - Multiple offers: status = needs_review
    - 0 offers: status = no_offers
    """
    for line in lines:
        offers = line.get("offers", [])
        if len(offers) == 1:
            line["status"] = "decided"
            line["selected_offer_id"] = offers[0]["id"]
            line["sell_price"] = offers[0]["unit_price"]
            line["sell_price_manual"] = False
        elif len(offers) > 1:
            line["status"] = "needs_review"
            line["selected_offer_id"] = None
        else:
            line["status"] = "no_offers"
            line["selected_offer_id"] = None


def build_excel_export(
    line_items: list[dict],
    quote_number: str,
    customer_name: str,
) -> bytes:
    """Generate a styled Excel workbook from quote line items.

    Returns raw bytes.
    """
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = quote_number or "Quote"

    header_fill = PatternFill(start_color="3D6895", end_color="3D6895", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF", size=11)

    columns = [
        ("MPN", 20),
        ("Manufacturer", 18),
        ("Qty", 10),
        ("Unit Price", 14),
        ("Extended Price", 16),
        ("Lead Time", 14),
        ("Date Codes", 14),
        ("Condition", 12),
        ("Packaging", 14),
        ("MOQ", 10),
        ("Vendor", 20),
    ]

    for col_idx, (name, width) in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    for row_idx, item in enumerate(line_items, 2):
        qty = item.get("qty") or 0
        sell = item.get("sell_price") or 0
        ws.cell(row=row_idx, column=1, value=item.get("mpn", ""))
        ws.cell(row=row_idx, column=2, value=item.get("manufacturer", ""))
        ws.cell(row=row_idx, column=3, value=qty)
        price_cell = ws.cell(row=row_idx, column=4, value=sell)
        price_cell.number_format = "$#,##0.0000"
        ext_cell = ws.cell(row=row_idx, column=5, value=round(qty * sell, 2))
        ext_cell.number_format = "$#,##0.00"
        ws.cell(row=row_idx, column=6, value=item.get("lead_time", ""))
        ws.cell(row=row_idx, column=7, value=item.get("date_code", ""))
        ws.cell(row=row_idx, column=8, value=item.get("condition", ""))
        ws.cell(row=row_idx, column=9, value=item.get("packaging", ""))
        ws.cell(row=row_idx, column=10, value=item.get("moq", ""))
        ws.cell(row=row_idx, column=11, value=item.get("vendor_name", ""))

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def save_quote_from_builder(
    db: Session,
    req_id: int,
    payload,
    user,
) -> dict:
    """Create or revise a Quote from the builder's save payload.

    If payload.quote_id is set, marks the old quote as 'revised' and creates a new
    revision with the updated line items. Otherwise creates a fresh quote.

    Fires the same hooks as create_quote: requisition state transition,
    per-requirement sourcing status update, and knowledge ledger capture.
    """
    from loguru import logger

    from app.constants import QuoteStatus
    from app.models import Quote, QuoteLine, Requisition
    from app.services.crm_service import next_quote_number

    req = db.get(Requisition, req_id)
    if not req:
        raise ValueError("Requisition not found")

    line_items = []
    for li in payload.lines:
        line_items.append(
            {
                "mpn": li.mpn,
                "manufacturer": li.manufacturer,
                "qty": li.qty,
                "cost_price": li.cost_price,
                "sell_price": li.sell_price,
                "margin_pct": li.margin_pct,
                "lead_time": li.lead_time,
                "date_code": li.date_code,
                "condition": li.condition,
                "packaging": li.packaging,
                "moq": li.moq,
                "offer_id": li.offer_id,
                "material_card_id": li.material_card_id,
                "notes": li.notes,
            }
        )

    total_sell = sum(li["qty"] * li["sell_price"] for li in line_items)
    total_cost = sum(li["qty"] * li["cost_price"] for li in line_items)
    margin_pct = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0

    # Handle revision
    revision = 1
    quote_number = next_quote_number(db)
    if payload.quote_id:
        old_quote = db.get(Quote, payload.quote_id)
        if old_quote:
            old_revision = old_quote.revision or 1
            quote_number = old_quote.quote_number
            revision = old_revision + 1
            old_quote.quote_number = f"{quote_number}-R{old_revision}"
            old_quote.status = QuoteStatus.REVISED

    # Advance requisition status to "quoting" if appropriate
    if req.status in ("active", "sourcing", "offers"):
        try:
            from app.services.requisition_state import transition as req_transition

            req_transition(req, "quoting", user, db)
        except ValueError:
            pass  # already in quoting or later state

    quote = Quote(
        requisition_id=req_id,
        customer_site_id=req.customer_site_id,
        quote_number=quote_number,
        revision=revision,
        line_items=line_items,
        subtotal=total_sell,
        total_cost=total_cost,
        total_margin_pct=margin_pct,
        payment_terms=payload.payment_terms,
        shipping_terms=payload.shipping_terms,
        validity_days=payload.validity_days,
        notes=payload.notes,
        created_by_id=user.id,
    )
    db.add(quote)
    db.flush()

    for li in line_items:
        db.add(
            QuoteLine(
                quote_id=quote.id,
                material_card_id=li.get("material_card_id"),
                offer_id=li.get("offer_id"),
                mpn=li.get("mpn", ""),
                manufacturer=li.get("manufacturer"),
                qty=li.get("qty"),
                cost_price=li.get("cost_price"),
                sell_price=li.get("sell_price"),
                margin_pct=li.get("margin_pct"),
                currency="USD",
            )
        )

    db.commit()

    # Fire per-requirement sourcing status hook (same as create_quote)
    try:
        from app.services.requirement_status import on_quote_built

        offer_ids = [li.get("offer_id") for li in line_items if li.get("offer_id")]
        if offer_ids:
            from app.models import Offer as OfferModel

            offers_used = db.query(OfferModel).filter(OfferModel.id.in_(offer_ids)).all()
            requirement_ids = list({o.requirement_id for o in offers_used if o.requirement_id})
            if requirement_ids:
                on_quote_built(requirement_ids, db, actor=user)
                db.commit()
    except Exception as e:
        logger.warning("Requirement status update (on_quote_built) failed: {}", e)

    # Knowledge ledger capture (same as create_quote)
    try:
        from app.services.knowledge_service import capture_quote_fact

        capture_quote_fact(db, quote=quote, user_id=user.id)
    except Exception as e:
        logger.warning("Knowledge auto-capture (quote) failed: {}", e)

    return {
        "ok": True,
        "quote_id": quote.id,
        "quote_number": quote.quote_number,
        "revision": quote.revision,
    }

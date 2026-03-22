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
        active_offers = [o for o in r.offers if o.status == "active"]
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
                            "cost": lq.get("cost_price"),
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

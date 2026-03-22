# tests/test_quote_builder.py
"""tests/test_quote_builder.py — Quote Builder service, schemas, and endpoint tests.

Called by: pytest
Depends on: app.schemas.quote_builder, app.services.quote_builder_service, conftest.py
"""

from app.schemas.quote_builder import QuoteBuilderLine, QuoteBuilderSaveRequest


def test_builder_line_schema_valid():
    line = QuoteBuilderLine(
        requirement_id=1,
        offer_id=42,
        mpn="LM358DR",
        manufacturer="TI",
        qty=500,
        cost_price=0.24,
        sell_price=0.31,
        margin_pct=22.6,
    )
    assert line.mpn == "LM358DR"
    assert line.cost_price == 0.24


def test_builder_line_schema_optional_fields():
    line = QuoteBuilderLine(
        requirement_id=1,
        mpn="LM358DR",
        manufacturer="TI",
        qty=500,
        cost_price=0.24,
        sell_price=0.31,
        margin_pct=22.6,
    )
    assert line.offer_id is None
    assert line.lead_time is None
    assert line.notes is None


def test_builder_save_request_valid():
    req = QuoteBuilderSaveRequest(
        lines=[
            QuoteBuilderLine(
                requirement_id=1,
                mpn="LM358DR",
                manufacturer="TI",
                qty=500,
                cost_price=0.24,
                sell_price=0.31,
                margin_pct=22.6,
            )
        ],
        payment_terms="Net 30",
        shipping_terms="FCA",
        validity_days=7,
    )
    assert len(req.lines) == 1
    assert req.payment_terms == "Net 30"


def test_builder_save_request_empty_lines_rejected():
    import pytest as _pt

    with _pt.raises(Exception):
        QuoteBuilderSaveRequest(
            lines=[],
            payment_terms="Net 30",
        )


from app.services.quote_builder_service import apply_smart_defaults


def _mock_requirement(req_id, mpn, offers_count, target_qty=100, target_price=1.0):
    """Build a mock requirement dict as returned by get_builder_data."""
    offers = []
    for i in range(offers_count):
        offers.append(
            {
                "id": req_id * 100 + i,
                "vendor_name": f"Vendor{i}",
                "unit_price": 0.50 + i * 0.10,
                "qty_available": 500,
                "lead_time": "2 weeks",
                "date_code": "2024+",
                "condition": "new",
                "packaging": None,
                "moq": 100,
                "confidence": 0.95 if i == 0 else None,
                "notes": None,
            }
        )
    return {
        "requirement_id": req_id,
        "mpn": mpn,
        "manufacturer": "TI",
        "target_qty": target_qty,
        "target_price": target_price,
        "customer_pn": None,
        "date_codes": None,
        "condition": None,
        "packaging": None,
        "firmware": None,
        "hardware_codes": None,
        "sale_notes": None,
        "need_by_date": None,
        "offers": offers,
        "offer_count": offers_count,
        "status": "unknown",
        "selected_offer_id": None,
        "sell_price": None,
        "sell_price_manual": False,
        "buyer_notes": "",
        "pricing_history": None,
    }


def test_smart_defaults_single_offer_auto_decided():
    lines = [_mock_requirement(1, "LM358DR", 1)]
    apply_smart_defaults(lines)
    assert lines[0]["status"] == "decided"
    assert lines[0]["selected_offer_id"] == 100


def test_smart_defaults_multiple_offers_needs_review():
    lines = [_mock_requirement(2, "LM317T", 3)]
    apply_smart_defaults(lines)
    assert lines[0]["status"] == "needs_review"
    assert lines[0]["selected_offer_id"] is None


def test_smart_defaults_no_offers():
    lines = [_mock_requirement(3, "NE555P", 0)]
    apply_smart_defaults(lines)
    assert lines[0]["status"] == "no_offers"
    assert lines[0]["selected_offer_id"] is None


def test_smart_defaults_auto_pick_sets_sell_price():
    lines = [_mock_requirement(4, "SN74HC00N", 1)]
    apply_smart_defaults(lines)
    assert lines[0]["sell_price"] == lines[0]["offers"][0]["unit_price"]
    assert lines[0]["sell_price_manual"] is False


from app.services.quote_builder_service import build_excel_export


def test_excel_export_produces_valid_xlsx():
    line_items = [
        {
            "mpn": "LM358DR",
            "manufacturer": "TI",
            "qty": 500,
            "sell_price": 0.31,
            "lead_time": "2 weeks",
            "date_code": "2024+",
            "condition": "new",
            "packaging": "Tape & Reel",
            "moq": 100,
            "vendor_name": "DigiKey",
        }
    ]
    xlsx_bytes = build_excel_export(
        line_items=line_items,
        quote_number="Q-2026-0042",
        customer_name="Acme Electronics",
    )
    assert isinstance(xlsx_bytes, bytes)
    assert len(xlsx_bytes) > 100
    assert xlsx_bytes[:2] == b"PK"


def test_excel_export_has_correct_columns():
    from io import BytesIO

    import openpyxl

    line_items = [
        {
            "mpn": "LM358DR",
            "manufacturer": "TI",
            "qty": 500,
            "sell_price": 0.31,
            "lead_time": "2 weeks",
            "date_code": "2024+",
            "condition": "new",
            "packaging": "T&R",
            "moq": 100,
            "vendor_name": "DigiKey",
        }
    ]
    xlsx_bytes = build_excel_export(line_items, "Q-2026-0042", "Acme")
    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes))
    ws = wb.active
    headers = [cell.value for cell in ws[1]]
    assert "MPN" in headers
    assert "Manufacturer" in headers
    assert "Qty" in headers
    assert "Unit Price" in headers
    assert "Extended Price" in headers
    assert "Lead Time" in headers
    assert "Vendor" in headers
    assert ws.cell(row=2, column=1).value == "LM358DR"

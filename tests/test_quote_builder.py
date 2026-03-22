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

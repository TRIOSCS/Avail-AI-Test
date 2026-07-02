"""test_integration_quote_workflow.py — Integration tests for the quote lifecycle.

Called by: pytest
Depends on: conftest.py (client, db_session, test_requisition, test_offer fixtures)
"""

# ── Tests ────────────────────────────────────────────────────────────────


def test_create_quote_from_offers_populates_line_items_json(client, db_session, test_requisition, test_offer):
    """P0 regression (OQ-01): creating a quote from selected offers must populate
    quote.line_items (the JSON the emailed quote + PDF render from) — not only the
    QuoteLine ORM rows the detail UI reads.

    Otherwise the customer receives an empty line-item table.
    """
    from app.models import Quote

    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/create-quote",
        data={"offer_ids": str(test_offer.id)},
    )
    assert resp.status_code == 200

    quote = (
        db_session.query(Quote).filter(Quote.requisition_id == test_requisition.id).order_by(Quote.id.desc()).first()
    )
    assert quote is not None
    assert quote.line_items, "quote.line_items must be populated (email/PDF render from it)"
    assert len(quote.line_items) == 1
    li = quote.line_items[0]
    assert li["mpn"] == "LM317T"
    assert li["sell_price"] == 0.50
    assert li["qty"] == 1000
    assert li["offer_id"] == test_offer.id
    assert "manufacturer" in li

# tests/test_quote_builder.py
"""tests/test_quote_builder.py — Quote-builder service + schema tests.

Covers the QuoteBuilderLine/SaveRequest schemas, apply_smart_defaults (still used
by the buy-plan seeding path), and save_quote_from_builder (create / revise /
source inheritance). The modal-era endpoint tests left with their routes in the
Wave 3 consolidation (see test_quote_builder_router.py for the deleted-route sweep).

Called by: pytest
Depends on: app.schemas.quote_builder, app.services.quote_builder_service, conftest.py
"""

import pytest

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
    with pytest.raises(Exception):
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


@pytest.mark.parametrize(
    ("req_id", "mpn", "offers_count", "expected_status", "expected_offer_id"),
    [
        pytest.param(1, "LM358DR", 1, "decided", 100, id="single_offer_auto_decided"),
        pytest.param(2, "LM317T", 3, "needs_review", None, id="multiple_offers_needs_review"),
        pytest.param(3, "NE555P", 0, "no_offers", None, id="no_offers"),
    ],
)
def test_smart_defaults_status(req_id, mpn, offers_count, expected_status, expected_offer_id):
    lines = [_mock_requirement(req_id, mpn, offers_count)]
    apply_smart_defaults(lines)
    assert lines[0]["status"] == expected_status
    assert lines[0]["selected_offer_id"] == expected_offer_id


def test_smart_defaults_auto_pick_sets_sell_price():
    lines = [_mock_requirement(4, "SN74HC00N", 1)]
    apply_smart_defaults(lines)
    assert lines[0]["sell_price"] == lines[0]["offers"][0]["unit_price"]
    assert lines[0]["sell_price_manual"] is False


from app.models import Company, CustomerSite, Quote, Requirement, Requisition


def _seed_requirement(db_session, test_user):
    """Seed Acme Corp → HQ site → active requisition → one LM358DR requirement.

    Returns (requisition, requirement).
    """
    company = Company(name="Acme Corp")
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    req = Requisition(name="Test Req", customer_site_id=site.id, created_by=test_user.id, status="open")
    db_session.add(req)
    db_session.flush()
    r1 = Requirement(requisition_id=req.id, primary_mpn="LM358DR", manufacturer="TI", target_qty=500)
    db_session.add(r1)
    db_session.commit()
    return req, r1


def test_save_quote_from_builder_creates_quote(db_session, test_user):
    """Uses conftest fixtures for DB session and user."""
    from app.schemas.quote_builder import QuoteBuilderLine, QuoteBuilderSaveRequest
    from app.services.quote_builder_service import save_quote_from_builder

    req, r1 = _seed_requirement(db_session, test_user)

    payload = QuoteBuilderSaveRequest(
        lines=[
            QuoteBuilderLine(
                requirement_id=r1.id,
                mpn="LM358DR",
                manufacturer="TI",
                qty=500,
                cost_price=0.24,
                sell_price=0.31,
                margin_pct=22.6,
            )
        ],
        payment_terms="Net 30",
    )
    result = save_quote_from_builder(db_session, req_id=req.id, payload=payload, user=test_user)
    assert result["ok"] is True
    assert "quote_id" in result
    assert "quote_number" in result

    # Verify Quote record exists
    quote = db_session.get(Quote, result["quote_id"])
    assert quote is not None
    assert quote.payment_terms == "Net 30"
    assert len(quote.line_items) == 1


def test_save_quote_revision(db_session, test_user):
    """Uses conftest fixtures for DB session and user."""
    from app.schemas.quote_builder import QuoteBuilderLine, QuoteBuilderSaveRequest
    from app.services.quote_builder_service import save_quote_from_builder

    req, r1 = _seed_requirement(db_session, test_user)

    # First save
    payload1 = QuoteBuilderSaveRequest(
        lines=[
            QuoteBuilderLine(
                requirement_id=r1.id,
                mpn="LM358DR",
                manufacturer="TI",
                qty=500,
                cost_price=0.24,
                sell_price=0.31,
                margin_pct=22.6,
            )
        ],
    )
    result1 = save_quote_from_builder(db_session, req_id=req.id, payload=payload1, user=test_user)
    quote_id_1 = result1["quote_id"]

    # Second save (revision) — same quote_id passed
    payload2 = QuoteBuilderSaveRequest(
        lines=[
            QuoteBuilderLine(
                requirement_id=r1.id,
                mpn="LM358DR",
                manufacturer="TI",
                qty=500,
                cost_price=0.24,
                sell_price=0.40,
                margin_pct=40.0,
            )
        ],
        quote_id=quote_id_1,
    )
    result2 = save_quote_from_builder(db_session, req_id=req.id, payload=payload2, user=test_user)
    assert result2["ok"] is True
    assert result2["quote_id"] != quote_id_1  # New quote for revision

    # Old quote should be "revised"
    old_quote = db_session.get(Quote, quote_id_1)
    assert old_quote.status == "revised"


def test_save_quote_revision_inherits_source(db_session, test_user):
    """A revision must inherit ``source`` from its parent (Wave 6 revenue attribution).

    A fresh (non-revision) build keeps the default ``source`` of None.
    """
    from app.schemas.quote_builder import QuoteBuilderLine, QuoteBuilderSaveRequest
    from app.services.quote_builder_service import save_quote_from_builder

    req, r1 = _seed_requirement(db_session, test_user)

    def _payload(quote_id=None):
        return QuoteBuilderSaveRequest(
            lines=[
                QuoteBuilderLine(
                    requirement_id=r1.id,
                    mpn="LM358DR",
                    manufacturer="TI",
                    qty=500,
                    cost_price=0.24,
                    sell_price=0.31,
                    margin_pct=22.6,
                )
            ],
            quote_id=quote_id,
        )

    # Fresh build → no proactive origin → source stays None.
    result1 = save_quote_from_builder(db_session, req_id=req.id, payload=_payload(), user=test_user)
    parent = db_session.get(Quote, result1["quote_id"])
    assert parent.source is None

    # Mark the parent as proactive-originated, then revise it.
    parent.source = "proactive"
    db_session.commit()

    result2 = save_quote_from_builder(db_session, req_id=req.id, payload=_payload(quote_id=parent.id), user=test_user)
    revision = db_session.get(Quote, result2["quote_id"])
    assert revision.id != parent.id
    assert revision.source == "proactive"  # attribution propagates through the revision


def test_builder_pdf_export_404_bad_quote(client):
    resp = client.get("/v2/partials/quote-builder/1/export/pdf?quote_id=99999")
    assert resp.status_code == 404

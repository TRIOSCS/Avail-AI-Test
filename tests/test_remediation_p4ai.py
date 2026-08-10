"""tests/test_remediation_p4ai.py — QC 2026-08-10 P4 (AI provenance: offer currency).

The AI-parsed-offer review form never collected the currency the reviewer sees, so save
stamped every offer USD even when the vendor priced in EUR/GBP — a wrong currency then
flowed into customer quotes. The form now collects + persists it.
"""

from tests.conftest import engine  # noqa: F401


def test_parse_offer_form_rows_collects_currency():
    from app.services.ai_offer_service import parse_offer_form_rows

    form = {
        "offers[0].mpn": "LM317T",
        "offers[0].unit_price": "0.42",
        "offers[0].currency": "EUR",
    }
    rows = parse_offer_form_rows(form, "Acme")
    assert rows[0]["currency"] == "EUR"  # was dropped before


def test_saved_offer_keeps_reviewed_currency(db_session, test_user):
    from app.models import Offer, Requisition
    from app.services.ai_offer_service import save_form_parsed_offers

    req = Requisition(name="REQ-CUR", status="open", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    rows = [{"mpn": "LM317T", "vendor_name": "Acme", "qty_available": 100, "unit_price": 0.42, "currency": "EUR"}]
    save_form_parsed_offers(db_session, req.id, "Acme", rows, test_user)
    db_session.commit()
    offer = db_session.query(Offer).filter(Offer.mpn == "LM317T").one()
    assert offer.currency == "EUR"  # not silently USD


def test_missing_currency_defaults_usd(db_session, test_user):
    from app.models import Offer, Requisition
    from app.services.ai_offer_service import save_form_parsed_offers

    req = Requisition(name="REQ-CUR2", status="open", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    rows = [{"mpn": "NE555P", "vendor_name": "Acme", "qty_available": 50, "unit_price": 0.10, "currency": None}]
    save_form_parsed_offers(db_session, req.id, "Acme", rows, test_user)
    db_session.commit()
    offer = db_session.query(Offer).filter(Offer.mpn == "NE555P").one()
    assert offer.currency == "USD"  # sane default preserved

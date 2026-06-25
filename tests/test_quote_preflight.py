"""tests/test_quote_preflight.py — Tests for app/services/quote_preflight.py.

Covers the three advisory checks (dnc / country_of_origin / mpn_drift), the clean path,
and the never-blocks contract.
"""

from sqlalchemy.orm import Session

from app.models import Offer, Quote, QuoteLine, Requirement, SiteContact
from app.services.quote_preflight import PreflightWarning, quote_preflight


def _add_requirement(db: Session, requisition_id: int, **kw) -> Requirement:
    r = Requirement(requisition_id=requisition_id, **kw)
    db.add(r)
    db.commit()
    return r


def _add_line(db: Session, quote: Quote, mpn: str, offer_id: int | None = None) -> QuoteLine:
    ql = QuoteLine(quote_id=quote.id, mpn=mpn, offer_id=offer_id, qty=1)
    db.add(ql)
    db.commit()
    db.refresh(quote)
    return ql


def _codes(warnings: list[PreflightWarning]) -> set[str]:
    return {w.code for w in warnings}


class TestPreflightClean:
    def test_quote_with_matching_mpn_no_offer_coo_no_dnc_is_clean(self, db_session, test_quote):
        _add_requirement(db_session, test_quote.requisition_id, primary_mpn="LM317T")
        _add_line(db_session, test_quote, mpn="LM317T")
        assert quote_preflight(db_session, test_quote) == []

    def test_returns_list_of_preflight_warnings(self, db_session, test_quote):
        # Contract: always a list, never raises / blocks — even with no requisition MPNs.
        result = quote_preflight(db_session, test_quote)
        assert isinstance(result, list)


class TestDncCheck:
    def test_dnc_site_flagged(self, db_session, test_quote, test_customer_site):
        test_customer_site.do_not_contact = True
        db_session.commit()
        assert "dnc" in _codes(quote_preflight(db_session, test_quote))

    def test_dnc_contact_matching_recipient_flagged(self, db_session, test_quote, test_customer_site):
        # A contact at the site, same email as the site recipient, marked DNC.
        db_session.add(
            SiteContact(
                customer_site_id=test_customer_site.id,
                full_name="Jane Doe",
                email=test_customer_site.contact_email,
                do_not_contact=True,
            )
        )
        db_session.commit()
        assert "dnc" in _codes(quote_preflight(db_session, test_quote))

    def test_non_dnc_contact_not_flagged(self, db_session, test_quote, test_customer_site):
        db_session.add(
            SiteContact(
                customer_site_id=test_customer_site.id,
                full_name="Jane Doe",
                email=test_customer_site.contact_email,
                do_not_contact=False,
            )
        )
        db_session.commit()
        assert "dnc" not in _codes(quote_preflight(db_session, test_quote))


class TestCountryOfOriginCheck:
    def _offer_with_coo(self, db_session, test_quote, coo):
        offer = Offer(
            requisition_id=test_quote.requisition_id,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=100,
            unit_price=0.5,
            status="active",
            country_of_origin=coo,
        )
        db_session.add(offer)
        db_session.commit()
        _add_line(db_session, test_quote, mpn="LM317T", offer_id=offer.id)

    def test_non_us_coo_flagged(self, db_session, test_quote):
        self._offer_with_coo(db_session, test_quote, "China")
        assert "country_of_origin" in _codes(quote_preflight(db_session, test_quote))

    def test_us_coo_not_flagged(self, db_session, test_quote):
        self._offer_with_coo(db_session, test_quote, "United States")
        assert "country_of_origin" not in _codes(quote_preflight(db_session, test_quote))

    def test_usa_abbrev_not_flagged(self, db_session, test_quote):
        self._offer_with_coo(db_session, test_quote, "U.S.A.")
        assert "country_of_origin" not in _codes(quote_preflight(db_session, test_quote))

    def test_blank_coo_not_flagged(self, db_session, test_quote):
        self._offer_with_coo(db_session, test_quote, "")
        assert "country_of_origin" not in _codes(quote_preflight(db_session, test_quote))


class TestMpnDriftCheck:
    def test_drift_flagged_when_line_mpn_absent_from_requirements(self, db_session, test_quote):
        _add_requirement(db_session, test_quote.requisition_id, primary_mpn="LM317T")
        _add_line(db_session, test_quote, mpn="TOTALLY-DIFFERENT-9999")
        warnings = quote_preflight(db_session, test_quote)
        assert "mpn_drift" in _codes(warnings)

    def test_no_drift_when_line_matches_requirement(self, db_session, test_quote):
        _add_requirement(db_session, test_quote.requisition_id, customer_pn="CUST-PN-1", primary_mpn="LM317T")
        _add_line(db_session, test_quote, mpn="lm317t")  # case-insensitive via normalize_mpn
        assert "mpn_drift" not in _codes(quote_preflight(db_session, test_quote))

    def test_no_drift_check_when_requisition_has_no_mpns(self, db_session, test_user):
        # A requisition whose requirement carries no MPN at all → nothing to compare
        # against, so drift is never asserted (avoids false positives on MPN-less reqs).
        from app.models import Requisition

        req = Requisition(name="REQ-NO-MPN", customer_name="X", status="active", created_by=test_user.id)
        db_session.add(req)
        db_session.flush()
        db_session.add(Requirement(requisition_id=req.id, target_qty=1))  # no primary/customer/oem PN
        quote = Quote(requisition_id=req.id, quote_number="NO-MPN-Q-1", status="draft", line_items=[])
        db_session.add(quote)
        db_session.commit()
        db_session.refresh(quote)
        _add_line(db_session, quote, mpn="ANYTHING")
        assert "mpn_drift" not in _codes(quote_preflight(db_session, quote))


class TestPreflightEndpoint:
    def test_endpoint_returns_warnings_json(self, client, db_session, test_quote, test_customer_site):
        test_customer_site.do_not_contact = True
        db_session.commit()
        resp = client.get(f"/api/quotes/{test_quote.id}/preflight")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        assert any(w["code"] == "dnc" for w in data["warnings"])
        assert all(w["level"] == "warning" for w in data["warnings"])

    def test_endpoint_clean_quote_returns_empty(self, client, db_session, test_quote):
        _add_requirement(db_session, test_quote.requisition_id, primary_mpn="LM317T")
        _add_line(db_session, test_quote, mpn="LM317T")
        resp = client.get(f"/api/quotes/{test_quote.id}/preflight")
        assert resp.status_code == 200
        assert resp.json() == {"warnings": [], "count": 0}

    def test_endpoint_404_for_missing_quote(self, client):
        resp = client.get("/api/quotes/999999/preflight")
        assert resp.status_code == 404
        assert "error" in resp.json()  # project error envelope, not "detail"


def test_multiple_warnings_accumulate_and_never_block(db_session, test_quote, test_customer_site):
    """All three issues at once → three advisory warnings, returned (not raised)."""
    test_customer_site.do_not_contact = True
    db_session.commit()
    _add_requirement(db_session, test_quote.requisition_id, primary_mpn="LM317T")
    offer = Offer(
        requisition_id=test_quote.requisition_id,
        vendor_name="Arrow",
        mpn="FOREIGN-1",
        qty_available=10,
        unit_price=1.0,
        status="active",
        country_of_origin="Malaysia",
    )
    db_session.add(offer)
    db_session.commit()
    _add_line(db_session, test_quote, mpn="FOREIGN-1", offer_id=offer.id)

    warnings = quote_preflight(db_session, test_quote)
    assert _codes(warnings) == {"dnc", "country_of_origin", "mpn_drift"}
    assert all(w.to_dict()["level"] == "warning" for w in warnings)

"""
tests/test_cph_hooks.py -- Tests for Phase 2.6 purchase history growth hooks.

Covers: CPH upsert on quote won, CPH upsert on offer status → won.

Called by: pytest
Depends on: app/routers/crm/quotes.py, app/routers/crm/offers.py, conftest.py
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import (
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    Quote,
    Requirement,
    Requisition,
    User,
)
from app.models.purchase_history import CustomerPartHistory

# ── Helpers ─────────────────────────────────────────────────────────


def _make_card(db: Session, mpn: str = "LM317T") -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower().strip(), display_mpn=mpn)
    db.add(card)
    db.flush()
    return card


def _setup_quote_scenario(db: Session, user: User, company: Company, site: CustomerSite):
    """Create a requisition + quote with material_card_id in line items."""
    card = _make_card(db)
    req = Requisition(
        name="REQ-CPH-TEST",
        customer_site_id=site.id,
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        material_card_id=card.id,
        target_qty=100,
    )
    db.add(item)
    db.flush()

    quote = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number="Q-CPH-001",
        status="sent",
        subtotal=150.00,
        created_by_id=user.id,
        line_items=[
            {
                "mpn": "LM317T",
                "material_card_id": card.id,
                "qty": 100,
                "sell_price": 1.50,
                "cost_price": 0.80,
                "margin_pct": 46.7,
            }
        ],
    )
    db.add(quote)
    db.commit()
    return req, quote, card


# ── Quote Won → CPH Hook ───────────────────────────────────────────


class TestQuoteWonCPHHook:
    def test_quote_won_creates_cph(self, client, db_session, test_user, test_company, test_customer_site):
        """Marking a quote as won creates a CPH record."""
        req, quote, card = _setup_quote_scenario(db_session, test_user, test_company, test_customer_site)
        resp = client.post(
            f"/api/quotes/{quote.id}/result",
            json={"result": "won"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify CPH was created
        cph = (
            db_session.query(CustomerPartHistory)
            .filter_by(
                company_id=test_company.id,
                material_card_id=card.id,
                source="avail_quote_won",
            )
            .first()
        )
        assert cph is not None
        assert cph.purchase_count == 1
        assert float(cph.last_unit_price) == 1.50
        assert cph.source_ref == f"quote:{quote.id}"

    def test_quote_lost_no_cph(self, client, db_session, test_user, test_company, test_customer_site):
        """Marking a quote as lost does NOT create CPH."""
        req, quote, card = _setup_quote_scenario(db_session, test_user, test_company, test_customer_site)
        resp = client.post(
            f"/api/quotes/{quote.id}/result",
            json={"result": "lost", "reason": "price"},
        )
        assert resp.status_code == 200

        cph = (
            db_session.query(CustomerPartHistory)
            .filter_by(company_id=test_company.id, material_card_id=card.id)
            .first()
        )
        assert cph is None

    def test_quote_won_no_card_in_line_items(self, client, db_session, test_user, test_company, test_customer_site):
        """Quote line items without material_card_id are skipped."""
        req = Requisition(
            name="REQ-NOCARD",
            customer_site_id=test_customer_site.id,
            status="active",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.flush()
        quote = Quote(
            requisition_id=req.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-NOCARD",
            status="sent",
            subtotal=50.00,
            created_by_id=test_user.id,
            line_items=[{"mpn": "NOCARD", "qty": 10, "sell_price": 5.0}],
        )
        db_session.add(quote)
        db_session.commit()

        resp = client.post(
            f"/api/quotes/{quote.id}/result",
            json={"result": "won"},
        )
        assert resp.status_code == 200

        cph_count = db_session.query(CustomerPartHistory).filter_by(company_id=test_company.id).count()
        assert cph_count == 0

    def test_quote_won_upsert_increments(self, client, db_session, test_user, test_company, test_customer_site):
        """Winning same quote twice increments purchase_count."""
        req, quote, card = _setup_quote_scenario(db_session, test_user, test_company, test_customer_site)
        # First win
        resp = client.post(
            f"/api/quotes/{quote.id}/result",
            json={"result": "won"},
        )
        assert resp.status_code == 200

        # Reset quote status to allow re-winning
        quote.status = "sent"
        quote.result = None
        db_session.commit()

        # Second win
        resp = client.post(
            f"/api/quotes/{quote.id}/result",
            json={"result": "won"},
        )
        assert resp.status_code == 200

        cph = (
            db_session.query(CustomerPartHistory)
            .filter_by(
                company_id=test_company.id,
                material_card_id=card.id,
                source="avail_quote_won",
            )
            .first()
        )
        assert cph is not None
        assert cph.purchase_count == 2

    def test_quote_won_req_no_site_graceful(self, client, db_session, test_user, test_customer_site):
        """Quote won where requisition has no customer_site_id → CPH hook skips
        gracefully."""
        req = Requisition(
            name="REQ-NO-SITE",
            status="active",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.flush()
        # Quote still needs customer_site_id (NOT NULL), but req doesn't have one
        quote = Quote(
            requisition_id=req.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-NOSITE",
            status="sent",
            subtotal=50.00,
            created_by_id=test_user.id,
            line_items=[{"mpn": "X1", "qty": 10, "sell_price": 5.0}],
        )
        db_session.add(quote)
        db_session.commit()

        resp = client.post(
            f"/api/quotes/{quote.id}/result",
            json={"result": "won"},
        )
        # Should succeed — CPH hook gracefully skips when req has no customer_site_id
        assert resp.status_code == 200


# ── Offer Won → CPH Hook ───────────────────────────────────────────


class TestOfferWonCPHHook:
    def test_offer_won_creates_cph(self, client, db_session, test_user, test_company, test_customer_site):
        """Updating offer status to 'won' creates a CPH record."""
        card = _make_card(db_session, "LM7805")
        req = Requisition(
            name="REQ-OFFER-WON",
            customer_site_id=test_customer_site.id,
            status="active",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Arrow",
            mpn="LM7805",
            material_card_id=card.id,
            qty_available=500,
            unit_price=0.75,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.put(
            f"/api/offers/{offer.id}",
            json={"status": "won"},
        )
        assert resp.status_code == 200

        cph = (
            db_session.query(CustomerPartHistory)
            .filter_by(
                company_id=test_company.id,
                material_card_id=card.id,
                source="avail_offer",
            )
            .first()
        )
        assert cph is not None
        assert cph.purchase_count == 1
        assert float(cph.last_unit_price) == 0.75
        assert cph.source_ref == f"offer:{offer.id}"

    def test_offer_status_active_no_cph(self, client, db_session, test_user, test_company, test_customer_site):
        """Updating offer to non-won status doesn't create CPH."""
        card = _make_card(db_session, "SN7400")
        req = Requisition(
            name="REQ-OFFER-ACTIVE",
            customer_site_id=test_customer_site.id,
            status="active",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Mouser",
            mpn="SN7400",
            material_card_id=card.id,
            qty_available=100,
            unit_price=0.30,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.put(
            f"/api/offers/{offer.id}",
            json={"notes": "Updated notes"},
        )
        assert resp.status_code == 200

        cph_count = (
            db_session.query(CustomerPartHistory)
            .filter_by(company_id=test_company.id, material_card_id=card.id)
            .count()
        )
        assert cph_count == 0

    def test_offer_already_won_no_double_cph(self, client, db_session, test_user, test_company, test_customer_site):
        """Updating an already-won offer doesn't trigger CPH again."""
        card = _make_card(db_session, "NE555")
        req = Requisition(
            name="REQ-OFFER-ALREADY",
            customer_site_id=test_customer_site.id,
            status="active",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="DigiKey",
            mpn="NE555",
            material_card_id=card.id,
            qty_available=200,
            unit_price=0.40,
            entered_by_id=test_user.id,
            status="won",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        # Update notes on already-won offer
        resp = client.put(
            f"/api/offers/{offer.id}",
            json={"notes": "New note", "status": "won"},
        )
        assert resp.status_code == 200

        cph_count = (
            db_session.query(CustomerPartHistory)
            .filter_by(company_id=test_company.id, material_card_id=card.id)
            .count()
        )
        assert cph_count == 0

    def test_offer_won_no_card_skipped(self, client, db_session, test_user, test_customer_site):
        """Offer without material_card_id doesn't trigger CPH."""
        req = Requisition(
            name="REQ-NOCARD-OFFER",
            customer_site_id=test_customer_site.id,
            status="active",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Arrow",
            mpn="NOCARD",
            qty_available=100,
            unit_price=1.00,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.put(
            f"/api/offers/{offer.id}",
            json={"status": "won"},
        )
        assert resp.status_code == 200

    def test_offer_won_no_site_no_error(self, client, db_session, test_user):
        """Offer won with no customer site doesn't error."""
        card = _make_card(db_session, "XTAL1")
        req = Requisition(
            name="REQ-NOSITE-OFFER",
            status="active",
            created_by=test_user.id,
        )
        db_session.add(req)
        db_session.flush()
        offer = Offer(
            requisition_id=req.id,
            vendor_name="Arrow",
            mpn="XTAL1",
            material_card_id=card.id,
            qty_available=50,
            unit_price=2.00,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.put(
            f"/api/offers/{offer.id}",
            json={"status": "won"},
        )
        assert resp.status_code == 200

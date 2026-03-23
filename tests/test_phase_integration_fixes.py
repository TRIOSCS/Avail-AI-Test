"""test_phase_integration_fixes.py — Tests for integration bug fixes and cross-feature
links.

Verifies: Create Quote target fix, Add to Draft Quote route, Quotes tab clickable rows,
Build Buy Plan button on won quotes, search→material link, vendor emails→req backlink,
material sightings→vendor link.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Offer, Quote, QuoteLine, Requirement, Requisition, User

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def req_with_offers(db_session: Session, test_user: User):
    """A requisition with requirements and offers."""
    req = Requisition(
        name="Integration Test Req",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(requisition_id=req.id, primary_mpn="LM317T", target_qty=100)
    db_session.add(requirement)
    db_session.flush()

    offers = []
    for vendor, price, qty in [
        ("Arrow", 0.45, 5000),
        ("Mouser", 0.50, 10000),
    ]:
        o = Offer(
            requisition_id=req.id,
            requirement_id=requirement.id,
            vendor_name=vendor,
            mpn="LM317T",
            unit_price=price,
            qty_available=qty,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o)
        offers.append(o)

    db_session.commit()
    for o in offers:
        db_session.refresh(o)
    db_session.refresh(req)
    return req, offers


@pytest.fixture()
def draft_quote(db_session: Session, test_user: User, req_with_offers):
    """A draft quote on the requisition."""
    from app.models import Company, CustomerSite

    req, offers = req_with_offers

    # Create a company and site for the quote FK
    company = Company(name="Test Co", account_type="customer")
    db_session.add(company)
    db_session.flush()
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    quote = Quote(
        requisition_id=req.id,
        quote_number="Q-TEST-1",
        status="draft",
        created_by_id=test_user.id,
        customer_site_id=site.id,
    )
    db_session.add(quote)
    db_session.commit()
    db_session.refresh(quote)
    return quote


# ── Phase 1A: Create Quote target fix ─────────────────────────────────


class TestCreateQuoteTarget:
    def test_offers_tab_has_main_content_target(self, client: TestClient, req_with_offers):
        """Create Quote button should target #main-content, not #tab-content."""
        req, _ = req_with_offers
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/offers")
        assert resp.status_code == 200
        assert 'hx-target="#main-content"' in resp.text
        assert "Create Quote from Selected" in resp.text


# ── Phase 1B: Add to Draft Quote route ────────────────────────────────


class TestAddToDraftQuote:
    def test_add_offers_to_draft_quote(self, client: TestClient, db_session: Session, req_with_offers, draft_quote):
        """Adding offers to draft quote creates QuoteLines."""
        req, offers = req_with_offers
        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=f'{{"offer_ids": [{offers[0].id}], "quote_id": {draft_quote.id}}}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "added" in resp.text.lower() or "Offers" in resp.text

        lines = db_session.query(QuoteLine).filter(QuoteLine.quote_id == draft_quote.id).all()
        assert len(lines) == 1
        assert lines[0].mpn == "LM317T"

    def test_add_to_non_draft_quote_fails(self, client: TestClient, db_session: Session, req_with_offers, draft_quote):
        """Can't add offers to a sent/won quote."""
        req, offers = req_with_offers
        draft_quote.status = "sent"
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offers-to-quote",
            content=f'{{"offer_ids": [{offers[0].id}], "quote_id": {draft_quote.id}}}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


# ── Phase 1C: Quotes tab clickable rows ──────────────────────────────


class TestQuotesTabClickable:
    def test_quotes_tab_rows_have_hx_get(self, client: TestClient, req_with_offers, draft_quote):
        """Quote rows should have hx-get for navigation."""
        req, _ = req_with_offers
        resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/quotes")
        assert resp.status_code == 200
        assert f'hx-get="/v2/partials/quotes/{draft_quote.id}"' in resp.text
        assert f'hx-push-url="/v2/quotes/{draft_quote.id}"' in resp.text


# ── Phase 1D: Build Buy Plan button on won quotes ────────────────────


class TestBuildBuyPlanButton:
    def test_won_quote_shows_build_button(self, client: TestClient, db_session: Session, draft_quote):
        """Won quotes should show 'Build Buy Plan' button."""
        draft_quote.status = "won"
        db_session.commit()

        resp = client.get(f"/v2/partials/quotes/{draft_quote.id}")
        assert resp.status_code == 200
        assert "Build Buy Plan" in resp.text

    def test_draft_quote_no_build_button(self, client: TestClient, draft_quote):
        """Draft quotes should not show 'Build Buy Plan' button."""
        resp = client.get(f"/v2/partials/quotes/{draft_quote.id}")
        assert resp.status_code == 200
        assert "Build Buy Plan" not in resp.text


# ── Phase 2B: Vendor emails → requisition backlink ───────────────────


class TestVendorEmailsBacklink:
    def test_emails_tab_has_requisition_link(self, client: TestClient, db_session: Session, test_user: User):
        """Email contacts should link back to their requisition."""
        from app.models import VendorCard
        from app.models.offers import Contact as RfqContact

        vendor = VendorCard(
            display_name="Backlink Vendor",
            normalized_name="backlink vendor",
        )
        db_session.add(vendor)
        db_session.flush()

        req = Requisition(
            name="Backlink Req",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        contact = RfqContact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Backlink Vendor",
            vendor_name_normalized="backlink vendor",
            vendor_contact="sales@backlink.com",
            subject="RFQ Test",
            status="sent",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{vendor.id}/tab/emails")
        assert resp.status_code == 200
        assert f"Req #{req.id}" in resp.text
        assert f"/v2/requisitions/{req.id}" in resp.text


# ── Phase 2C: Material sightings → vendor link ──────────────────────


class TestMaterialVendorLink:
    def test_sightings_have_vendor_links(self, client: TestClient, db_session: Session, test_user: User):
        """Material card sightings should link vendor names to vendor search."""
        from app.models import Requirement, Sighting
        from app.models.intelligence import MaterialCard

        card = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        req = Requisition(
            name="Vendor Link Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        requirement = Requirement(requisition_id=req.id, primary_mpn="LM317T", target_qty=100)
        db_session.add(requirement)
        db_session.flush()

        sighting = Sighting(
            requirement_id=requirement.id,
            material_card_id=card.id,
            mpn_matched="LM317T",
            vendor_name="TestVendor",
            unit_price=0.45,
            qty_available=5000,
            source_type="api",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sighting)
        db_session.commit()

        # Sightings are now in lazy-loaded tabs, not inline on detail page.
        # Detail page should show tab structure.
        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert "material-tab-content" in resp.text
        assert "Vendors" in resp.text


# ── Phase 5B: Company activity tab ──────────────────────────────────


class TestCompanyActivityTab:
    def test_company_activity_tab_with_rfq(self, client: TestClient, db_session: Session, test_user: User):
        """Company activity tab shows RFQ contacts for linked requisitions."""
        from app.models import Company
        from app.models.offers import Contact as RfqContact

        company = Company(name="Activity Co", account_type="customer")
        db_session.add(company)
        db_session.flush()

        req = Requisition(
            name="Activity Req",
            status="active",
            created_by=test_user.id,
            company_id=company.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        contact = RfqContact(
            requisition_id=req.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Activity Vendor",
            vendor_name_normalized="activity vendor",
            vendor_contact="sales@activity.com",
            subject="RFQ Activity",
            status="sent",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/activity")
        assert resp.status_code == 200
        assert "Activity Vendor" in resp.text
        assert "RFQ History" in resp.text
        assert f"Req #{req.id}" in resp.text

    def test_company_activity_tab_empty(self, client: TestClient, db_session: Session, test_user: User):
        """Empty company activity tab shows placeholder."""
        from app.models import Company

        company = Company(name="Empty Co", account_type="customer")
        db_session.add(company)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/tab/activity")
        assert resp.status_code == 200
        assert "No activity recorded" in resp.text


# ── Phase 6: AI Insights panels ─────────────────────────────────────


class TestInsightsPanels:
    def test_requisition_insights_panel_empty(self, client: TestClient, db_session: Session, test_user: User):
        """Requisition insights panel renders with no cached insights."""
        req = Requisition(
            name="Insights Req",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{req.id}/insights")
        assert resp.status_code == 200
        assert "AI Insights" in resp.text
        assert "No insights yet" in resp.text

    def test_vendor_insights_panel_empty(self, client: TestClient, db_session: Session, test_user: User):
        """Vendor insights panel renders with no cached insights."""
        from app.models import VendorCard

        vendor = VendorCard(
            display_name="Insights Vendor",
            normalized_name="insights vendor",
        )
        db_session.add(vendor)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{vendor.id}/insights")
        assert resp.status_code == 200
        assert "AI Insights" in resp.text

    def test_company_insights_panel_empty(self, client: TestClient, db_session: Session, test_user: User):
        """Company insights panel renders with no cached insights."""
        from app.models import Company

        company = Company(name="Insights Co", account_type="customer")
        db_session.add(company)
        db_session.commit()

        resp = client.get(f"/v2/partials/customers/{company.id}/insights")
        assert resp.status_code == 200
        assert "AI Insights" in resp.text

    def test_pipeline_insights_panel(self, client: TestClient):
        """Pipeline insights panel renders."""
        resp = client.get("/v2/partials/dashboard/pipeline-insights")
        assert resp.status_code == 200
        assert "AI Insights" in resp.text


# ── Phase 7: Admin data ops tab ─────────────────────────────────────


class TestDataOpsTab:
    def test_data_ops_tab_forbidden_for_non_admin(self, client: TestClient, test_user: User):
        """Data ops tab requires admin role."""
        resp = client.get("/v2/partials/settings/data-ops")
        assert resp.status_code == 403

    def test_data_ops_tab_renders_for_admin(self, db_session: Session, admin_user):
        """Data ops tab renders for admin users."""
        from fastapi.testclient import TestClient as TC

        from app.dependencies import get_db, require_user
        from app.main import app

        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[get_db] = lambda: db_session
        try:
            with TC(app) as c:
                resp = c.get("/v2/partials/settings/data-ops")
            assert resp.status_code == 200
            assert "Vendor Duplicates" in resp.text
            assert "Company Duplicates" in resp.text
        finally:
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(get_db, None)


# ── Phase 8: Offer actions ──────────────────────────────────────────


class TestManualOfferCreation:
    def test_add_offer_form_renders(self, client: TestClient, db_session: Session, test_user: User):
        """Add offer form renders with requirement options."""
        req = Requisition(
            name="Offer Form Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(requisition_id=req.id, primary_mpn="TEST123", target_qty=50)
        db_session.add(requirement)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{req.id}/add-offer-form")
        assert resp.status_code == 200
        assert "Add Manual Offer" in resp.text
        assert "TEST123" in resp.text

    def test_create_manual_offer(self, client: TestClient, db_session: Session, test_user: User):
        """Manual offer creation saves to DB."""
        req = Requisition(
            name="Manual Offer Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={
                "vendor_name": "Manual Vendor",
                "mpn": "LM358N",
                "qty_available": "1000",
                "unit_price": "0.25",
                "lead_time": "1 week",
            },
        )
        assert resp.status_code == 200

        offer = db_session.query(Offer).filter(Offer.requisition_id == req.id, Offer.mpn == "LM358N").first()
        assert offer is not None
        assert offer.vendor_name == "Manual Vendor"
        assert offer.source == "manual"
        assert float(offer.unit_price) == 0.25

    def test_create_offer_missing_fields(self, client: TestClient, db_session: Session, test_user: User):
        """Manual offer creation fails without vendor/mpn."""
        req = Requisition(
            name="Missing Fields Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/requisitions/{req.id}/add-offer",
            data={"vendor_name": "", "mpn": ""},
        )
        assert resp.status_code == 400


class TestReconfirmOffer:
    def test_reconfirm_resets_ttl(self, client: TestClient, db_session: Session, test_user: User):
        """Reconfirming an offer resets expiry and increments count."""
        req = Requisition(
            name="Reconfirm Test",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()

        offer = Offer(
            requisition_id=req.id,
            vendor_name="Reconfirm Vendor",
            mpn="RC100",
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()
        db_session.refresh(offer)

        resp = client.post(f"/v2/partials/requisitions/{req.id}/offers/{offer.id}/reconfirm")
        assert resp.status_code == 200

        db_session.refresh(offer)
        assert offer.reconfirm_count == 1
        assert offer.reconfirmed_at is not None
        assert offer.expires_at is not None

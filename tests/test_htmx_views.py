"""
tests/test_htmx_views.py — Tests for the HTMX + Alpine.js MVP frontend views.

Covers full page loads, HTMX partial requests, requisition CRUD,
vendor/company listing and detail, and search form rendering.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, etc.)
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import Company, Contact, CustomerSite, Offer, Quote, Requirement, Requisition, Sighting, User, VendorCard
from app.models.vendors import VendorContact


# ── Full page loads ──────────────────────────────────────────────────────


class TestFullPageLoads:
    """Test that /v2 routes serve the base page with correct initial partial URL."""

    def test_v2_root_redirects_to_requisitions(self, client: TestClient):
        resp = client.get("/v2")
        assert resp.status_code == 200
        assert "AvailAI" in resp.text

    def test_v2_requisitions_page(self, client: TestClient):
        resp = client.get("/v2/requisitions")
        assert resp.status_code == 200
        assert "AvailAI" in resp.text

    def test_v2_search_page(self, client: TestClient):
        resp = client.get("/v2/search")
        assert resp.status_code == 200
        assert "AvailAI" in resp.text

    def test_v2_vendors_page(self, client: TestClient):
        resp = client.get("/v2/vendors")
        assert resp.status_code == 200
        assert "AvailAI" in resp.text

    def test_v2_companies_page(self, client: TestClient):
        resp = client.get("/v2/companies")
        assert resp.status_code == 200
        assert "AvailAI" in resp.text


class TestLoginPage:
    """Test that unauthenticated users see the login page."""

    def test_v2_unauthenticated_shows_login(self, db_session):
        """Unauthenticated request to /v2 should show login page."""
        from app.database import get_db
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        # Don't override require_user — let get_user return None
        with TestClient(app) as c:
            resp = c.get("/v2")
            assert resp.status_code == 200
            assert "Sign in with Microsoft" in resp.text

        app.dependency_overrides.clear()


# ── Requisition partials ────────────────────────────────────────────────


class TestRequisitionPartials:
    """Test requisition list, detail, and create partials."""

    def test_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions")
        assert resp.status_code == 200
        assert "No requisitions found" in resp.text

    def test_list_with_data(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions")
        assert resp.status_code == 200
        assert test_requisition.name in resp.text

    def test_list_search_filter(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions?q=REQ-TEST")
        assert resp.status_code == 200
        assert test_requisition.name in resp.text

    def test_list_search_no_match(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions?q=NONEXISTENT")
        assert resp.status_code == 200
        assert "No requisitions found" in resp.text

    def test_list_status_filter(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions?status=open")
        assert resp.status_code == 200
        assert test_requisition.name in resp.text

    def test_detail(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}")
        assert resp.status_code == 200
        assert test_requisition.name in resp.text
        assert "LM317T" in resp.text  # requirement MPN

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999")
        assert resp.status_code == 404

    def test_create_requisition(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={
                "name": "New HTMX Req",
                "customer_name": "Test Co",
                "deadline": "2026-04-01",
                "urgency": "hot",
                "parts_text": "LM358N, 500\nTL074CN, 200",
            },
        )
        assert resp.status_code == 200
        assert "New HTMX Req" in resp.text

    def test_create_requisition_minimal(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={"name": "Minimal Req", "customer_name": "", "deadline": "", "urgency": "normal", "parts_text": ""},
        )
        assert resp.status_code == 200

    def test_add_requirement(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/requirements",
            data={"primary_mpn": "NE555P", "target_qty": 1000, "brand": "Texas Instruments"},
        )
        assert resp.status_code == 200
        assert "NE555P" in resp.text

    def test_add_requirement_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/requirements",
            data={"primary_mpn": "NE555P", "target_qty": 1},
        )
        assert resp.status_code == 404


# ── Search partials ─────────────────────────────────────────────────────


class TestSearchPartials:
    """Test search form and search execution."""

    def test_search_form(self, client: TestClient):
        resp = client.get("/v2/partials/search")
        assert resp.status_code == 200
        assert "Part Search" in resp.text
        assert "Search All Sources" in resp.text

    def test_search_empty_mpn(self, client: TestClient):
        resp = client.post("/v2/partials/search/run", data={"mpn": ""})
        assert resp.status_code == 200
        assert "Please enter a part number" in resp.text


# ── Vendor partials ─────────────────────────────────────────────────────


class TestVendorPartials:
    """Test vendor list and detail partials."""

    def test_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "No vendors found" in resp.text

    def test_list_with_data(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert test_vendor_card.display_name in resp.text

    def test_list_search(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get("/v2/partials/vendors?q=arrow")
        assert resp.status_code == 200
        assert test_vendor_card.display_name in resp.text

    def test_list_search_no_match(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get("/v2/partials/vendors?q=zzzznonexistent")
        assert resp.status_code == 200
        assert "No vendors found" in resp.text

    def test_detail(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}")
        assert resp.status_code == 200
        assert test_vendor_card.display_name in resp.text
        assert "Sightings" in resp.text

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999")
        assert resp.status_code == 404

    def test_detail_with_contacts(self, client: TestClient, test_vendor_card: VendorCard, db_session):
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            full_name="John Doe",
            title="Sales Manager",
            email="john@arrow.com",
            phone="+1-555-0101",
            source="manual",
        )
        db_session.add(vc)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}")
        assert resp.status_code == 200
        assert "John Doe" in resp.text
        assert "Sales Manager" in resp.text


# ── Company partials ────────────────────────────────────────────────────


class TestCompanyPartials:
    """Test company list and detail partials."""

    def test_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/companies")
        assert resp.status_code == 200
        assert "No companies found" in resp.text

    def test_list_with_data(self, client: TestClient, test_company: Company):
        resp = client.get("/v2/partials/companies")
        assert resp.status_code == 200
        assert test_company.name in resp.text

    def test_list_search(self, client: TestClient, test_company: Company):
        resp = client.get("/v2/partials/companies?search=Acme")
        assert resp.status_code == 200
        assert test_company.name in resp.text

    def test_list_search_no_match(self, client: TestClient, test_company: Company):
        resp = client.get("/v2/partials/companies?search=zzzznonexistent")
        assert resp.status_code == 200
        assert "No companies found" in resp.text

    def test_detail(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/companies/{test_company.id}")
        assert resp.status_code == 200
        assert test_company.name in resp.text

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/companies/99999")
        assert resp.status_code == 404

    def test_detail_with_sites(self, client: TestClient, test_company: Company, db_session):
        site = CustomerSite(
            company_id=test_company.id,
            site_name="Acme HQ",
            site_type="HQ",
            city="New York",
            country="US",
            is_active=True,
        )
        db_session.add(site)
        db_session.commit()

        resp = client.get(f"/v2/partials/companies/{test_company.id}")
        assert resp.status_code == 200
        assert "Acme HQ" in resp.text


# ── Dashboard partial ───────────────────────────────────────────────────


class TestDashboardPartial:
    """Test dashboard stats partial."""

    def test_dashboard(self, client: TestClient):
        resp = client.get("/v2/partials/dashboard")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text
        assert "Open Requisitions" in resp.text

    def test_dashboard_counts(self, client: TestClient, test_requisition, test_vendor_card, test_company):
        resp = client.get("/v2/partials/dashboard")
        assert resp.status_code == 200
        # Should show non-zero counts
        assert "Active Vendors" in resp.text
        assert "Companies" in resp.text


# ── Root-level routes (Phase 4) ─────────────────────────────────────────


class TestRootLevelRoutes:
    """Test that / now serves the HTMX frontend."""

    def test_root_serves_htmx(self, client: TestClient):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "AvailAI" in resp.text
        assert "htmx" in resp.text.lower() or "hx-get" in resp.text

    def test_root_requisitions(self, client: TestClient):
        resp = client.get("/requisitions")
        assert resp.status_code == 200
        assert "AvailAI" in resp.text

    def test_root_search(self, client: TestClient):
        resp = client.get("/search")
        assert resp.status_code == 200

    def test_root_vendors(self, client: TestClient):
        resp = client.get("/vendors")
        assert resp.status_code == 200

    def test_root_companies(self, client: TestClient):
        resp = client.get("/companies")
        assert resp.status_code == 200

    def test_root_offers(self, client: TestClient):
        resp = client.get("/offers")
        assert resp.status_code == 200

    def test_root_quotes(self, client: TestClient):
        resp = client.get("/quotes")
        assert resp.status_code == 200

    def test_legacy_spa(self, client: TestClient):
        resp = client.get("/legacy")
        assert resp.status_code == 200


# ── RFQ panel ───────────────────────────────────────────────────────────


class TestRfqPanel:
    """Test RFQ activity panel in requisition detail."""

    def test_rfq_panel_empty(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/rfq")
        assert resp.status_code == 200
        assert "No RFQs sent yet" in resp.text

    def test_rfq_panel_with_contacts(self, client: TestClient, test_requisition: Requisition, test_user, db_session):
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Arrow Electronics",
            vendor_contact="sales@arrow.com",
            status="sent",
        )
        db_session.add(contact)
        db_session.commit()

        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/rfq")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text
        assert "Sent" in resp.text


# ── Offer partials ──────────────────────────────────────────────────────


class TestOfferPartials:
    """Test offer list partial."""

    def test_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/offers")
        assert resp.status_code == 200
        assert "No offers found" in resp.text

    def test_list_with_data(self, client: TestClient, test_requisition: Requisition, db_session):
        offer = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Arrow Electronics",
            mpn="LM317T",
            qty_available=5000,
            unit_price=0.45,
            status="active",
            source="manual",
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get("/v2/partials/offers")
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert "Arrow Electronics" in resp.text

    def test_list_filter_by_status(self, client: TestClient, test_requisition: Requisition, db_session):
        offer = Offer(
            requisition_id=test_requisition.id,
            vendor_name="Mouser",
            mpn="NE555P",
            status="active",
            source="manual",
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get("/v2/partials/offers?status=active")
        assert resp.status_code == 200
        assert "NE555P" in resp.text


# ── Quote partials ──────────────────────────────────────────────────────


class TestQuotePartials:
    """Test quote list and detail partials."""

    def test_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/quotes")
        assert resp.status_code == 200
        assert "No quotes found" in resp.text

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/quotes/99999")
        assert resp.status_code == 404

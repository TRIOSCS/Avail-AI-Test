"""Tests for Plan 2 core page HTMX routes.

Tests requisition list filters, bulk ops, tabs, create modal,
company tabs/enrich, vendor tabs/sort/blacklisted toggle.

Called by: pytest
Depends on: conftest.py fixtures, app/routers/htmx_views.py
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.models import Company, CustomerSite, Requirement, Requisition, VendorCard
from app.models.vendors import VendorContact

# ── Requisition filters and sorting ──────────────────────────────────────


class TestRequisitionFilters:
    """Test requisition list with new filter params."""

    def test_list_with_status_filter(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions?status=open")
        assert resp.status_code == 200

    def test_list_with_urgency_filter(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions?urgency=normal")
        assert resp.status_code == 200

    def test_list_with_owner_filter(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions?owner={test_requisition.created_by}")
        assert resp.status_code == 200
        assert test_requisition.name in resp.text

    def test_list_with_date_filter(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions?date_from=2020-01-01&date_to=2030-12-31")
        assert resp.status_code == 200

    def test_list_sort_by_name_asc(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions?sort=name&dir=asc")
        assert resp.status_code == 200

    def test_list_sort_by_created_at_desc(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions?sort=created_at&dir=desc")
        assert resp.status_code == 200

    def test_list_combined_filters(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions?status=open&urgency=normal&sort=name&dir=asc")
        assert resp.status_code == 200


# ── Requisition create modal ─────────────────────────────────────────────


class TestRequisitionCreateModal:
    """Test create form route and updated create behavior."""

    def test_create_form_returns_modal(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/create-form")
        assert resp.status_code == 200
        assert "New Requisition" in resp.text
        assert 'hx-post="/v2/partials/requisitions/create"' in resp.text

    def test_create_returns_single_row(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/create",
            data={
                "name": "Row Test Req",
                "customer_name": "Test Co",
                "deadline": "",
                "urgency": "hot",
                "parts_text": "LM358N, 500",
            },
        )
        assert resp.status_code == 200
        assert "Row Test Req" in resp.text
        # Should return a <tr> row, not the full list
        assert "<tr" in resp.text


# ── Requisition bulk actions ─────────────────────────────────────────────


class TestRequisitionBulkActions:
    """Test bulk archive, activate, assign."""

    def test_bulk_archive(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": str(test_requisition.id)},
        )
        assert resp.status_code == 200

    def test_bulk_activate(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            "/v2/partials/requisitions/bulk/activate",
            data={"ids": str(test_requisition.id)},
        )
        assert resp.status_code == 200

    def test_bulk_invalid_action(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            "/v2/partials/requisitions/bulk/delete",
            data={"ids": str(test_requisition.id)},
        )
        assert resp.status_code == 400

    def test_bulk_no_ids(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/bulk/archive",
            data={"ids": ""},
        )
        assert resp.status_code == 400


# ── Requisition tabs ─────────────────────────────────────────────────────


class TestRequisitionTabs:
    """Test requisition detail tab routes."""

    def test_tab_parts(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/parts")
        assert resp.status_code == 200
        assert "LM317T" in resp.text

    def test_tab_offers(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/offers")
        assert resp.status_code == 200
        assert "No offers" in resp.text

    def test_tab_offers_with_data(self, client: TestClient, test_requisition: Requisition, test_offer):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/offers")
        assert resp.status_code == 200
        assert "Arrow Electronics" in resp.text

    def test_tab_quotes(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/quotes")
        assert resp.status_code == 200

    def test_tab_buy_plans(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/buy_plans")
        assert resp.status_code == 200

    def test_tab_tasks(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/tasks")
        assert resp.status_code == 200
        assert "No tasks" in resp.text

    def test_tab_activity(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/activity")
        assert resp.status_code == 200
        assert "No activity" in resp.text

    def test_tab_invalid(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/invalid")
        assert resp.status_code == 404

    def test_tab_not_found_req(self, client: TestClient):
        resp = client.get("/v2/partials/requisitions/99999/tab/parts")
        assert resp.status_code == 404


# ── Delete requirement ───────────────────────────────────────────────────


class TestDeleteRequirement:
    """Test delete requirement route."""

    def test_delete_requirement(self, client: TestClient, test_requisition: Requisition, db_session):
        # Get the requirement ID
        req_item = db_session.query(Requirement).filter(Requirement.requisition_id == test_requisition.id).first()
        assert req_item is not None

        resp = client.delete(f"/v2/partials/requisitions/{test_requisition.id}/requirements/{req_item.id}")
        assert resp.status_code == 200
        assert resp.text == ""

    def test_delete_requirement_not_found(self, client: TestClient, test_requisition: Requisition):
        resp = client.delete(f"/v2/partials/requisitions/{test_requisition.id}/requirements/99999")
        assert resp.status_code == 404

    def test_delete_requirement_wrong_requisition(self, client: TestClient):
        resp = client.delete("/v2/partials/requisitions/99999/requirements/1")
        assert resp.status_code == 404


# ── Company tabs ─────────────────────────────────────────────────────────


class TestCompanyTabs:
    """Test company detail tab routes."""

    def test_tab_sites(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/companies/{test_company.id}/tab/sites")
        assert resp.status_code == 200

    def test_tab_sites_with_data(self, client: TestClient, test_company: Company, db_session):
        site = CustomerSite(
            company_id=test_company.id,
            site_name="Acme Branch",
            site_type="Branch",
            city="Boston",
            country="US",
            is_active=True,
        )
        db_session.add(site)
        db_session.commit()

        resp = client.get(f"/v2/partials/companies/{test_company.id}/tab/sites")
        assert resp.status_code == 200
        assert "Acme Branch" in resp.text

    def test_tab_contacts(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/companies/{test_company.id}/tab/contacts")
        assert resp.status_code == 200

    def test_tab_requisitions(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/companies/{test_company.id}/tab/requisitions")
        assert resp.status_code == 200

    def test_tab_activity(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/companies/{test_company.id}/tab/activity")
        assert resp.status_code == 200

    def test_tab_invalid(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/companies/{test_company.id}/tab/invalid")
        assert resp.status_code == 404

    def test_tab_not_found_company(self, client: TestClient):
        resp = client.get("/v2/partials/companies/99999/tab/sites")
        assert resp.status_code == 404


# ── Vendor list sorting and blacklisted ──────────────────────────────────


class TestVendorListFeatures:
    """Test vendor list with sort, blacklisted toggle."""

    def test_list_sort_by_name(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get("/v2/partials/vendors?sort=display_name&dir=asc")
        assert resp.status_code == 200
        assert test_vendor_card.display_name in resp.text

    def test_list_sort_by_sightings(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get("/v2/partials/vendors?sort=sighting_count&dir=desc")
        assert resp.status_code == 200

    def test_list_show_blacklisted(self, client: TestClient, test_vendor_card: VendorCard, db_session):
        # Create a blacklisted vendor
        bv = VendorCard(
            normalized_name="bad vendor",
            display_name="Bad Vendor",
            is_blacklisted=True,
            sighting_count=5,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bv)
        db_session.commit()

        # Default hides blacklisted
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "Bad Vendor" not in resp.text

        # Show blacklisted
        resp = client.get("/v2/partials/vendors?hide_blacklisted=false")
        assert resp.status_code == 200
        assert "Bad Vendor" in resp.text


# ── Vendor tabs ──────────────────────────────────────────────────────────


class TestVendorTabs:
    """Test vendor detail tab routes."""

    def test_tab_overview(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/overview")
        assert resp.status_code == 200

    def test_tab_contacts(self, client: TestClient, test_vendor_card: VendorCard, db_session):
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            full_name="Jane Contact",
            title="Buyer",
            email="jane@arrow.com",
            phone="+1-555-0300",
            source="manual",
        )
        db_session.add(vc)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/contacts")
        assert resp.status_code == 200
        assert "Jane Contact" in resp.text
        assert "tel:" in resp.text

    def test_tab_analytics(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/analytics")
        assert resp.status_code == 200
        assert "Win Rate" in resp.text

    def test_tab_offers(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/offers")
        assert resp.status_code == 200

    def test_tab_invalid(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/tab/invalid")
        assert resp.status_code == 404

    def test_tab_not_found_vendor(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999/tab/overview")
        assert resp.status_code == 404


# ── Brand color verification ─────────────────────────────────────────────


class TestBrandColors:
    """Verify brand palette usage in templates."""

    def test_requisitions_list_uses_brand(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/requisitions")
        assert resp.status_code == 200
        assert "brand-500" in resp.text
        assert "blue-600" not in resp.text

    def test_companies_list_uses_brand(self, client: TestClient, test_company: Company):
        resp = client.get("/v2/partials/companies")
        assert resp.status_code == 200
        assert "brand-500" in resp.text

    def test_vendors_list_uses_brand(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get("/v2/partials/vendors")
        assert resp.status_code == 200
        assert "brand-500" in resp.text

"""Integration tests for the unified requisition modal form.

Tests that both import-form and create-form return unified_modal.html, and that
import-save correctly handles indexed form fields submitted by the unified modal.

Called by: pytest
Depends on: tests/conftest.py (client, db_session, test_user fixtures)
"""

import pytest

from app.models import Company
from app.models.crm import CustomerSite


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param("/v2/partials/requisitions/import-form", id="import-form"),
        pytest.param("/v2/partials/requisitions/create-form", id="create-form"),
    ],
)
def test_unified_modal_renders(client, db_session, test_user, endpoint):
    """GET import-form and create-form both return the unified modal."""
    resp = client.get(endpoint)
    assert resp.status_code == 200
    assert "unifiedReqModal" in resp.text


def test_import_save_with_manufacturer(client, db_session, test_user):
    """Import-save works with the unified modal's indexed form fields."""
    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={
            "name": "Test Req",
            "customer_name": "",
            "customer_site_id": "",
            "deadline": "",
            "urgency": "normal",
            "reqs[0].primary_mpn": "LM317T",
            "reqs[0].manufacturer": "Texas Instruments",
            "reqs[0].target_qty": "500",
            "reqs[0].brand": "",
            "reqs[0].condition": "new",
            "reqs[0].target_price": "",
            "reqs[0].customer_pn": "",
            "reqs[0].substitutes": "",
        },
    )
    assert resp.status_code == 200


def test_import_parse_json_path_returns_data(client, monkeypatch):
    """POST import-parse?format=json returns JSON (used by unified modal's
    parseWithAI)."""
    mock_result = {
        "name": "Test RFQ",
        "customer_name": "Acme Corp",
        "requirements": [
            {"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": 500, "condition": "new"},
        ],
    }

    async def mock_parse(text):
        return mock_result

    monkeypatch.setattr("app.routers.htmx.requisitions.parse_freeform_rfq", mock_parse)
    resp = client.post(
        "/v2/partials/requisitions/import-parse?format=json",
        data={"name": "Test RFQ", "raw_text": "LM317T 500 TI"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "requirements" in data
    assert data["requirements"][0]["primary_mpn"] == "LM317T"


def test_import_parse_html_path_returns_unified_modal(client, monkeypatch):
    """POST import-parse (HTML path) now returns unified_modal.html, not
    import_preview."""
    mock_result = {
        "name": "Test RFQ",
        "customer_name": "",
        "requirements": [
            {"primary_mpn": "LM317T", "target_qty": 100, "condition": "new"},
        ],
    }

    async def mock_parse(text):
        return mock_result

    monkeypatch.setattr("app.routers.htmx.requisitions.parse_freeform_rfq", mock_parse)
    resp = client.post(
        "/v2/partials/requisitions/import-parse",
        data={"name": "Test RFQ", "raw_text": "LM317T 100"},
    )
    assert resp.status_code == 200
    assert "unifiedReqModal" in resp.text


class TestCustomerTypeaheadDropdown:
    """P5.2: GET /v2/partials/requisitions/customer-typeahead — the server-rendered hx-
    get dropdown that replaced customerPicker()'s client-side fetch-all + JS filter
    against /api/companies/typeahead (that JSON endpoint is untouched)."""

    def _company_with_site(self, db_session, name="Acme Electronics", site_name="HQ"):
        co = Company(name=name, is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name=site_name)
        db_session.add(site)
        db_session.flush()
        return co, site

    def test_empty_query_returns_top_companies(self, client, db_session, test_user):
        self._company_with_site(db_session)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions/customer-typeahead")
        assert resp.status_code == 200
        assert "Acme Electronics" in resp.text
        assert "+ New Customer" in resp.text

    def test_filters_by_q_case_insensitively(self, client, db_session, test_user):
        self._company_with_site(db_session, name="Widget Supply Co")
        self._company_with_site(db_session, name="Other Corp")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions/customer-typeahead?q=widget")
        assert resp.status_code == 200
        assert "Widget Supply Co" in resp.text
        assert "Other Corp" not in resp.text

    def test_single_site_company_click_selects_directly(self, client, db_session, test_user):
        co, site = self._company_with_site(db_session, site_name="Main Office")
        db_session.commit()
        resp = client.get("/v2/partials/requisitions/customer-typeahead?q=acme")
        assert resp.status_code == 200
        # tojson-quoted select(company, site) call — no double-quote-in-double-quote break.
        assert "@click='select(" in resp.text
        assert f'"id": {co.id}' in resp.text
        assert "Main Office" in resp.text

    def test_multi_site_company_lists_each_site(self, client, db_session, test_user):
        co = Company(name="Multi Site Inc", is_active=True)
        db_session.add(co)
        db_session.flush()
        db_session.add(CustomerSite(company_id=co.id, site_name="Dallas"))
        db_session.add(CustomerSite(company_id=co.id, site_name="Austin"))
        db_session.commit()
        resp = client.get("/v2/partials/requisitions/customer-typeahead?q=multi")
        assert resp.status_code == 200
        assert "Dallas" in resp.text
        assert "Austin" in resp.text

    def test_inactive_company_excluded(self, client, db_session, test_user):
        co = Company(name="Defunct Corp", is_active=False)
        db_session.add(co)
        db_session.commit()
        resp = client.get("/v2/partials/requisitions/customer-typeahead?q=defunct")
        assert resp.status_code == 200
        assert "Defunct Corp" not in resp.text


def test_import_save_multiple_parts(client, db_session, test_user):
    """Import-save creates a requisition with multiple requirements."""
    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={
            "name": "Multi-Part Req",
            "customer_name": "",
            "customer_site_id": "",
            "deadline": "",
            "urgency": "normal",
            "reqs[0].primary_mpn": "LM317T",
            "reqs[0].manufacturer": "Texas Instruments",
            "reqs[0].target_qty": "500",
            "reqs[0].condition": "new",
            "reqs[1].primary_mpn": "STM32F407VGT6",
            "reqs[1].manufacturer": "STMicroelectronics",
            "reqs[1].target_qty": "100",
            "reqs[1].condition": "new",
        },
    )
    assert resp.status_code == 200

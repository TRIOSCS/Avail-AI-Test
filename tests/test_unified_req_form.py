"""Integration tests for the unified requisition modal form.

Tests that both import-form and create-form return unified_modal.html, and that
import-save correctly handles indexed form fields submitted by the unified modal.

Called by: pytest
Depends on: tests/conftest.py (client, db_session, test_user fixtures)
"""


def test_unified_modal_renders(client, db_session, test_user):
    """GET import-form returns the unified modal."""
    resp = client.get("/v2/partials/requisitions/import-form")
    assert resp.status_code == 200
    assert "unifiedReqModal" in resp.text


def test_unified_modal_from_create_form(client, db_session, test_user):
    """GET create-form also returns the unified modal."""
    resp = client.get("/v2/partials/requisitions/create-form")
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

    monkeypatch.setattr("app.routers.htmx_views.parse_freeform_rfq", mock_parse)
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

    monkeypatch.setattr("app.routers.htmx_views.parse_freeform_rfq", mock_parse)
    resp = client.post(
        "/v2/partials/requisitions/import-parse",
        data={"name": "Test RFQ", "raw_text": "LM317T 100"},
    )
    assert resp.status_code == 200
    assert "unifiedReqModal" in resp.text


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

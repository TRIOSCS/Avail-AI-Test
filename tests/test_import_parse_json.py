"""Tests for JSON response mode on the import-parse route.

Verifies that GET /v2/partials/requisitions/import-parse?format=json returns
structured JSON instead of HTML for use by the unified modal Alpine.js component.

Calls: app/routers/htmx_views.py → requisition_import_parse
Depends on: conftest.py (client, db_session, test_user fixtures)
"""


def test_import_parse_json_format(client, db_session, monkeypatch):
    """Import-parse with format=json returns JSON with requirements list."""
    mock_result = {
        "name": "Test RFQ",
        "customer_name": "Acme Corp",
        "requirements": [
            {
                "primary_mpn": "LM317T",
                "target_qty": 500,
                "brand": "",
                "condition": "new",
                "notes": "",
            }
        ],
    }

    async def mock_parse(text):
        return mock_result

    monkeypatch.setattr("app.routers.htmx_views.parse_freeform_rfq", mock_parse)

    resp = client.post(
        "/v2/partials/requisitions/import-parse?format=json",
        data={"raw_text": "LM317T 500", "name": "Test"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "requirements" in data
    assert isinstance(data["requirements"], list)
    assert len(data["requirements"]) == 1
    assert data["requirements"][0]["primary_mpn"] == "LM317T"


def test_import_parse_json_inferred_fields(client, db_session, monkeypatch):
    """JSON response includes inferred_name and inferred_customer."""
    mock_result = {
        "name": "AI Inferred Name",
        "customer_name": "AI Customer",
        "requirements": [{"primary_mpn": "STM32F4", "target_qty": 10}],
    }

    async def mock_parse(text):
        return mock_result

    monkeypatch.setattr("app.routers.htmx_views.parse_freeform_rfq", mock_parse)

    # Pass a placeholder name so route accepts it; AI name will be used since user name is a space
    resp = client.post(
        "/v2/partials/requisitions/import-parse?format=json",
        data={"raw_text": "STM32F4 10", "name": " "},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["inferred_name"] == "AI Inferred Name"
    assert data["inferred_customer"] == "AI Customer"


def test_import_parse_json_empty_text(client, db_session):
    """Empty text returns error in JSON format without calling AI."""
    resp = client.post(
        "/v2/partials/requisitions/import-parse?format=json",
        data={"raw_text": "", "name": "Test"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["requirements"] == [] or "error" in data


def test_import_parse_html_format_unchanged(client, db_session, monkeypatch):
    """Without format=json, route still returns HTML (backward compat)."""
    mock_result = {
        "name": "Test RFQ",
        "customer_name": "",
        "requirements": [{"primary_mpn": "LM358DR", "target_qty": 100}],
    }

    async def mock_parse(text):
        return mock_result

    monkeypatch.setattr("app.routers.htmx_views.parse_freeform_rfq", mock_parse)

    resp = client.post(
        "/v2/partials/requisitions/import-parse",
        data={"raw_text": "LM358DR 100", "name": "Test"},
    )
    assert resp.status_code == 200
    # HTML response, not JSON
    assert "text/html" in resp.headers.get("content-type", "")
    assert "LM358DR" in resp.text


def test_import_parse_json_parse_failure(client, db_session, monkeypatch):
    """If AI returns None, JSON mode returns empty requirements list."""

    async def mock_parse_fail(text):
        return None

    monkeypatch.setattr("app.routers.htmx_views.parse_freeform_rfq", mock_parse_fail)

    resp = client.post(
        "/v2/partials/requisitions/import-parse?format=json",
        data={"raw_text": "garbage input that AI cannot parse", "name": "Test"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "requirements" in data
    assert data["requirements"] == []

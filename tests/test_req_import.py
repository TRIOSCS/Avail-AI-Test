"""Tests for requisition AI import (paste/upload → parse → save)."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_parse_freeform_rfq_returns_brand_and_condition():
    """Verify the parser schema accepts brand and condition fields."""
    mock_result = {
        "name": "Test RFQ",
        "requirements": [
            {
                "primary_mpn": "LM358DR",
                "target_qty": 500,
                "brand": "Texas Instruments",
                "condition": "new",
            }
        ],
    }
    with patch(
        "app.services.freeform_parser_service.routed_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        from app.services.freeform_parser_service import parse_freeform_rfq

        result = await parse_freeform_rfq("LM358DR x500 TI new")
        assert result is not None
        req = result["requirements"][0]
        assert req["brand"] == "Texas Instruments"
        assert req["condition"] == "new"


def test_import_parse_html_returns_unified_modal(client, monkeypatch):
    """POST /v2/partials/requisitions/import-parse (HTML path) returns unified modal."""
    mock_result = {
        "name": "Test RFQ",
        "customer_name": "Acme Corp",
        "requirements": [
            {"primary_mpn": "LM358DR", "target_qty": 500, "brand": "TI", "condition": "new"},
            {"primary_mpn": "STM32F407", "target_qty": 100, "condition": "new"},
        ],
    }

    async def mock_parse(text):
        return mock_result

    monkeypatch.setattr("app.routers.htmx_views.parse_freeform_rfq", mock_parse)
    resp = client.post(
        "/v2/partials/requisitions/import-parse",
        data={"name": "Test RFQ", "raw_text": "LM358DR 500 TI\nSTM32F407 100"},
    )
    assert resp.status_code == 200
    # HTML path now returns unified modal (Alpine.js-driven, not server-rendered rows)
    assert "unifiedReqModal" in resp.text


def test_import_save_creates_requisition(client):
    """POST /v2/partials/requisitions/import-save creates req + requirements."""
    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={
            "name": "Test Import",
            "customer_name": "Acme",
            "deadline": "",
            "urgency": "normal",
            "reqs[0].primary_mpn": "LM358DR",
            "reqs[0].target_qty": "500",
            "reqs[0].brand": "TI",
            "reqs[0].target_price": "0.85",
            "reqs[0].condition": "new",
            "reqs[0].notes": "",
            "reqs[1].primary_mpn": "STM32F407",
            "reqs[1].target_qty": "100",
            "reqs[1].brand": "",
            "reqs[1].target_price": "",
            "reqs[1].condition": "new",
            "reqs[1].notes": "",
        },
    )
    assert resp.status_code == 200
    assert "parts-list" in resp.text or "toast" in resp.text


@pytest.mark.asyncio
async def test_parse_freeform_rfq_empty_text():
    """Empty text returns None."""
    from app.services.freeform_parser_service import parse_freeform_rfq

    result = await parse_freeform_rfq("")
    assert result is None


@pytest.mark.asyncio
async def test_parse_freeform_rfq_normalizes_condition():
    """Condition normalization applied post-parse."""
    mock_result = {
        "name": "Test",
        "requirements": [
            {"primary_mpn": "LM358", "target_qty": 1, "condition": "NEW"},
        ],
    }
    with patch(
        "app.services.freeform_parser_service.routed_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        from app.services.freeform_parser_service import parse_freeform_rfq

        result = await parse_freeform_rfq("LM358 new")
        assert result["requirements"][0]["condition"] == "new"


def test_import_save_rejects_empty_parts(client):
    """Save with no valid parts shows error."""
    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={"name": "Empty", "customer_name": "", "deadline": "", "urgency": "normal"},
    )
    assert resp.status_code == 200


def test_import_form_loads(client):
    """GET import form returns 200."""
    resp = client.get("/v2/partials/requisitions/import-form")
    assert resp.status_code == 200
    assert "New Requisition" in resp.text


def test_company_lookup_form_accessible(client):
    """The lookup endpoint should return HTML."""
    with patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = client.post(
            "/v2/partials/companies/lookup",
            data={"company_name": "Test Corp", "location": "Dallas, TX"},
        )
        # Claude returns None in test → fallback message
        assert resp.status_code == 200


def test_company_quick_create(client, db_session):
    """Quick-create should create a company and site."""
    resp = client.post(
        "/v2/partials/companies/quick-create",
        data={
            "company_name": "Test Import Corp",
            "website": "testimportcorp.com",
            "phone": "555-0100",
            "address_line1": "123 Main St",
            "city": "Dallas",
            "state": "TX",
            "zip": "75201",
            "country": "US",
        },
    )
    assert resp.status_code == 200
    assert "Created" in resp.text or "already exists" in resp.text

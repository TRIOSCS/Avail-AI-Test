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


def test_import_parse_returns_preview(client, monkeypatch):
    """POST /v2/partials/requisitions/import-parse returns editable preview."""
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
    assert "LM358DR" in resp.text
    assert "STM32F407" in resp.text
    assert 'name="reqs[0].primary_mpn"' in resp.text


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

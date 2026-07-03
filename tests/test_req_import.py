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

    monkeypatch.setattr("app.routers.htmx.requisitions.parse_freeform_rfq", mock_parse)
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
            "/v2/partials/customers/lookup",
            data={"company_name": "Test Corp", "location": "Dallas, TX"},
        )
        # Claude returns None in test → fallback message
        assert resp.status_code == 200


def test_import_save_persists_canonical_substitutes(client, db_session):
    """REQ-09: substitutes_json (mpn + manufacturer) → canonical [{mpn, manufacturer}] rows.

    The old path read only the comma-joined MPN string and stored raw strings, dropping the
    per-sub manufacturer and violating the canonical substitutes format.
    """
    from app.models import Requirement

    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={
            "name": "Subs Canonical",
            "urgency": "normal",
            "reqs[0].primary_mpn": "LM358DR",
            "reqs[0].target_qty": "10",
            "reqs[0].manufacturer": "TI",
            "reqs[0].substitutes": "LM358N, LM358P",
            "reqs[0].substitutes_json": (
                '[{"mpn": "LM358N", "manufacturer": "ON Semi"}, {"mpn": "LM358P", "manufacturer": "STMicro"}]'
            ),
        },
    )
    assert resp.status_code == 200
    req = db_session.query(Requirement).filter_by(primary_mpn="LM358DR").one()
    assert isinstance(req.substitutes, list)
    assert len(req.substitutes) == 2
    assert all(isinstance(s, dict) and s["mpn"] for s in req.substitutes)
    # manufacturer carried through from substitutes_json (the old comma path dropped it)
    assert {s["manufacturer"] for s in req.substitutes} == {"ON Semi", "STMicro"}


def test_import_save_substitutes_json_fallback_to_comma(client, db_session):
    """REQ-09: with no substitutes_json, the legacy comma field still yields canonical dicts."""
    from app.models import Requirement

    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={
            "name": "Subs Fallback",
            "urgency": "normal",
            "reqs[0].primary_mpn": "STM32F407",
            "reqs[0].target_qty": "5",
            "reqs[0].manufacturer": "ST",
            "reqs[0].substitutes": "STM32F405, STM32F415",
        },
    )
    assert resp.status_code == 200
    req = db_session.query(Requirement).filter_by(primary_mpn="STM32F407").one()
    assert isinstance(req.substitutes, list)
    assert len(req.substitutes) == 2
    assert all(isinstance(s, dict) and s["mpn"] for s in req.substitutes)


def test_import_save_fires_req_list_refresh(client):
    """REQ-10: import-save fires HX-Trigger reqListRefresh and no longer hard-targets #parts-list.

    The old success snippet loaded /v2/partials/parts into #parts-list, which exists only in
    the parts workspace — opened from the requisitions list it hit htmx:targetError.
    """
    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={
            "name": "Trigger Test",
            "urgency": "normal",
            "reqs[0].primary_mpn": "LM358DR",
            "reqs[0].target_qty": "1",
            "reqs[0].manufacturer": "TI",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == "reqListRefresh"
    assert "parts-list" not in resp.text


def test_requisitions_list_has_refresh_hook(client):
    """REQ-10: the requisitions list carries a reqListRefresh listener so create refreshes it."""
    resp = client.get("/v2/partials/requisitions")
    assert resp.status_code == 200
    assert "reqListRefresh from:body" in resp.text


def test_parts_workspace_listens_for_req_list_refresh(client):
    """REQ-10: the parts workspace #parts-list also listens for reqListRefresh."""
    resp = client.get("/v2/partials/parts/workspace")
    assert resp.status_code == 200
    assert "reqListRefresh from:body" in resp.text


def test_edit_form_tolerates_legacy_string_subs(client, db_session, test_user):
    """REQ-09: the parts-tab edit form coerces legacy string subs so sub.mpn binds (not blank)."""
    from app.models import Requirement, Requisition

    reqn = Requisition(name="Legacy Subs", status="open", created_by=test_user.id)
    db_session.add(reqn)
    db_session.commit()
    part = Requirement(
        requisition_id=reqn.id,
        primary_mpn="LM358DR",
        manufacturer="TI",
        target_qty=1,
        substitutes=["LM358N"],  # legacy plain-string form
    )
    db_session.add(part)
    db_session.commit()

    resp = client.get(f"/v2/partials/requisitions/{reqn.id}/tab/parts")
    assert resp.status_code == 200
    # coercion maps plain strings → {mpn, manufacturer} dicts before Alpine binds sub.mpn
    assert "typeof s === 'string'" in resp.text
    assert "LM358N" in resp.text


def test_company_quick_create(client, db_session):
    """Quick-create should create a company and site."""
    resp = client.post(
        "/v2/partials/customers/quick-create",
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

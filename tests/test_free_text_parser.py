"""
test_free_text_parser.py — Tests for AI free-text RFQ/Offer parsing

Tests the parsing service, schemas, and router endpoints for the
freeform paste → AI parse → review → save flow.

Covers: parse_free_text service, freeform_parser_service, schema validation,
        parse/apply/save endpoints
"""

from unittest.mock import AsyncMock, patch

import pytest  # noqa: I001
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Service unit tests (legacy free_text_parser service)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_free_text_empty():
    """Empty text returns None."""
    from app.services.free_text_parser import parse_free_text

    result = await parse_free_text("")
    assert result is None

    result = await parse_free_text("   ")
    assert result is None


@pytest.mark.asyncio
async def test_parse_free_text_success():
    """Successful parse returns structured data with line_items."""
    mock_result = {
        "document_type": "rfq",
        "confidence": 0.9,
        "company_name": "Acme Corp",
        "contact_name": "John Doe",
        "contact_email": "john@acme.com",
        "notes": "Need ASAP",
        "line_items": [
            {"mpn": "LM358N", "quantity": 100, "target_price": 0.50, "condition": "new"},
            {"mpn": "NE555P", "quantity": 500},
        ],
    }

    with patch(
        "app.services.free_text_parser.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        from app.services.free_text_parser import parse_free_text

        result = await parse_free_text("Looking for LM358N x100 and NE555P x500")

    assert result is not None
    assert result["document_type"] == "rfq"
    assert result["confidence"] == 0.9
    assert len(result["line_items"]) == 2
    assert result["line_items"][0]["mpn"] == "LM358N"


@pytest.mark.asyncio
async def test_parse_free_text_offer():
    """Offer-type text is correctly classified."""
    mock_result = {
        "document_type": "offer",
        "confidence": 0.85,
        "company_name": "Parts Direct",
        "line_items": [
            {"mpn": "STM32F103", "quantity": 1000, "target_price": 2.50, "condition": "new"},
        ],
    }

    with patch(
        "app.services.free_text_parser.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        from app.services.free_text_parser import parse_free_text

        result = await parse_free_text("We have STM32F103 x1000 at $2.50 each")

    assert result["document_type"] == "offer"
    assert len(result["line_items"]) == 1


@pytest.mark.asyncio
async def test_parse_free_text_ai_failure():
    """AI failure returns None."""
    with patch(
        "app.services.free_text_parser.claude_structured",
        new_callable=AsyncMock,
        return_value=None,
    ):
        from app.services.free_text_parser import parse_free_text

        result = await parse_free_text("Some random text")

    assert result is None


@pytest.mark.asyncio
async def test_normalize_line_items():
    """Line item normalization applies condition/packaging cleanup."""
    from app.services.free_text_parser import _normalize_line_items

    result = {
        "line_items": [
            {"mpn": "  LM358N  ", "condition": "New", "packaging": "Tape & Reel", "quantity": 0},
            {"mpn": "NE555P", "currency": "EUR"},
        ]
    }
    _normalize_line_items(result)

    assert result["line_items"][0]["mpn"] == "LM358N"
    assert result["line_items"][0]["quantity"] == 1
    assert result["line_items"][1]["currency"] == "EUR"


# ---------------------------------------------------------------------------
# Schema validation tests (updated to match current schemas)
# ---------------------------------------------------------------------------


def test_freeform_rfq_request_requires_text():
    """ParseFreeformRfqRequest requires non-empty raw_text."""
    from pydantic import ValidationError

    from app.schemas.ai import ParseFreeformRfqRequest

    with pytest.raises(ValidationError):
        ParseFreeformRfqRequest(raw_text="")


def test_freeform_rfq_request_valid():
    """ParseFreeformRfqRequest accepts valid text."""
    from app.schemas.ai import ParseFreeformRfqRequest

    req = ParseFreeformRfqRequest(raw_text="LM358N x100")
    assert req.raw_text == "LM358N x100"


def test_freeform_offer_request_defaults():
    """ParseFreeformOfferRequest has sensible defaults."""
    from app.schemas.ai import ParseFreeformOfferRequest

    req = ParseFreeformOfferRequest(raw_text="We have LM358N x500")
    assert req.requisition_id is None


def test_apply_freeform_rfq_request_valid():
    """ApplyFreeformRfqRequest accepts valid payload."""
    from app.schemas.ai import ApplyFreeformRfqRequest

    req = ApplyFreeformRfqRequest(
        name="Test RFQ",
        customer_name="Acme",
        requirements=[{"primary_mpn": "LM358N", "target_qty": 100}],
    )
    assert req.name == "Test RFQ"
    assert len(req.requirements) == 1


def test_apply_freeform_rfq_request_empty_items():
    """ApplyFreeformRfqRequest rejects empty requirements list."""
    from pydantic import ValidationError

    from app.schemas.ai import ApplyFreeformRfqRequest

    with pytest.raises(ValidationError):
        ApplyFreeformRfqRequest(name="Test", requirements=[])


def test_save_freeform_offers_request_valid():
    """SaveFreeformOffersRequest accepts valid payload."""
    from app.schemas.ai import DraftOfferItem, SaveFreeformOffersRequest

    req = SaveFreeformOffersRequest(
        requisition_id=1,
        offers=[DraftOfferItem(mpn="STM32F103", vendor_name="Parts Direct")],
    )
    assert req.requisition_id == 1


def test_save_freeform_offers_bad_req_id():
    """SaveFreeformOffersRequest rejects non-positive requisition_id."""
    from pydantic import ValidationError

    from app.schemas.ai import DraftOfferItem, SaveFreeformOffersRequest

    with pytest.raises(ValidationError):
        SaveFreeformOffersRequest(
            requisition_id=0,
            offers=[DraftOfferItem(mpn="X")],
        )


# ---------------------------------------------------------------------------
# Router endpoint tests (updated routes)
# ---------------------------------------------------------------------------


@pytest.fixture()
def ft_test_user(db_session):
    """Buyer user for free-text endpoint tests."""
    from app.models import User

    user = User(
        email="ftbuyer@trioscs.com",
        name="FT Buyer",
        role="buyer",
        azure_id="ft-001",
        m365_connected=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def ft_client(db_session, ft_test_user):
    """TestClient with AI features enabled."""
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return ft_test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


def test_parse_freeform_rfq_endpoint_disabled(ft_client):
    """POST /api/ai/parse-freeform-rfq with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ft_client.post("/api/ai/parse-freeform-rfq", json={"raw_text": "LM358N x100"})
    assert resp.status_code == 403


def test_parse_freeform_rfq_endpoint_success(ft_client):
    """POST /api/ai/parse-freeform-rfq returns structured data."""
    mock_result = {
        "name": "Acme RFQ",
        "customer_name": "Acme",
        "requirements": [
            {"primary_mpn": "LM358N", "target_qty": 100, "target_price": 0.50},
        ],
    }

    with patch(
        "app.services.freeform_parser_service.routed_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = ft_client.post(
            "/api/ai/parse-freeform-rfq",
            json={"raw_text": "Need LM358N x100 at $0.50"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is True
    assert "template" in data


def test_parse_freeform_rfq_endpoint_no_result(ft_client):
    """POST /api/ai/parse-freeform-rfq with no extractable parts."""
    with patch(
        "app.services.freeform_parser_service.routed_structured",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = ft_client.post(
            "/api/ai/parse-freeform-rfq",
            json={"raw_text": "Hello, how are you?"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is False


def test_apply_freeform_rfq(ft_client, db_session):
    """POST /api/ai/apply-freeform-rfq creates requisition + requirements."""
    from app.models import Company, CustomerSite

    co = Company(name="Acme Corp", is_active=True)
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ", contact_name="J", contact_email="j@acme.com")
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)

    resp = ft_client.post(
        "/api/ai/apply-freeform-rfq",
        json={
            "name": "Acme RFQ",
            "customer_name": "Acme Corp",
            "customer_site_id": site.id,
            "requirements": [
                {"primary_mpn": "LM358N", "target_qty": 100, "target_price": 0.50},
                {"primary_mpn": "NE555P", "target_qty": 200},
            ],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["requirements_added"] == 2


def test_apply_freeform_rfq_empty_items(ft_client):
    """POST /api/ai/apply-freeform-rfq rejects empty requirements."""
    resp = ft_client.post(
        "/api/ai/apply-freeform-rfq",
        json={"name": "Test", "requirements": []},
    )
    assert resp.status_code == 422


def test_save_freeform_offers(ft_client, db_session):
    """POST /api/ai/save-freeform-offers creates offers on existing requisition."""
    from app.models import Requisition

    req = Requisition(name="Test Req", created_by=1, status="active")
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    resp = ft_client.post(
        "/api/ai/save-freeform-offers",
        json={
            "requisition_id": req.id,
            "offers": [
                {"mpn": "STM32F103", "vendor_name": "Parts Direct", "qty_available": 1000, "unit_price": 2.50},
            ],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] >= 1


def test_save_freeform_offers_missing_req(ft_client):
    """POST /api/ai/save-freeform-offers with bad requisition_id returns 404."""
    resp = ft_client.post(
        "/api/ai/save-freeform-offers",
        json={
            "requisition_id": 99999,
            "offers": [{"mpn": "X", "vendor_name": "Test"}],
        },
    )
    assert resp.status_code == 404

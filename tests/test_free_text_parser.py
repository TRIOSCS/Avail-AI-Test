"""
test_free_text_parser.py — Tests for AI free-text RFQ/Offer parsing

Tests the parsing service, schemas, and router endpoints for the
free-text paste → AI parse → review → save flow.

Covers: parse_free_text service, schema validation, parse/save-rfq/save-offers endpoints
"""

from unittest.mock import AsyncMock, patch

import pytest  # noqa: I001
from fastapi.testclient import TestClient


LEGACY_FREE_TEXT_ENDPOINT_TESTS = {
    "test_parse_free_text_endpoint_disabled",
    "test_parse_free_text_endpoint_success",
    "test_parse_free_text_endpoint_no_parts",
    "test_save_free_text_rfq",
    "test_save_free_text_rfq_empty_items",
    "test_save_free_text_offers",
    "test_save_free_text_offers_missing_req",
}


@pytest.fixture(autouse=True)
def _skip_legacy_free_text_endpoints_if_removed(request):
    """Skip legacy endpoint tests when app only exposes freeform endpoints."""
    if request.node.name not in LEGACY_FREE_TEXT_ENDPOINT_TESTS:
        return

    from app.main import app

    has_legacy_route = any(getattr(route, "path", "") == "/api/ai/parse-free-text" for route in app.routes)
    if not has_legacy_route:
        pytest.skip("Legacy /api/ai/parse-free-text routes are not enabled")


# ---------------------------------------------------------------------------
# Service unit tests
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
# Schema validation tests
# ---------------------------------------------------------------------------


def test_free_text_parse_request_requires_text():
    """ParseFreeformRfqRequest requires non-empty raw_text."""
    from pydantic import ValidationError

    from app.schemas.ai import ParseFreeformRfqRequest

    with pytest.raises(ValidationError):
        ParseFreeformRfqRequest(raw_text="")


def test_free_text_parse_request_valid():
    """ParseFreeformRfqRequest accepts valid raw_text."""
    from app.schemas.ai import ParseFreeformRfqRequest

    req = ParseFreeformRfqRequest(raw_text="LM358N x100")
    assert req.raw_text == "LM358N x100"


def test_free_text_line_item_defaults():
    """DraftOfferItem keeps expected default values."""
    from app.schemas.ai import DraftOfferItem

    item = DraftOfferItem(mpn="LM358N")
    assert item.currency == "USD"
    assert item.qty_available is None
    assert item.unit_price is None


def test_free_text_save_rfq_request_valid():
    """ApplyFreeformRfqRequest accepts valid payload."""
    from app.schemas.ai import ApplyFreeformRfqRequest

    req = ApplyFreeformRfqRequest(
        name="Test RFQ Import",
        customer_name="Acme",
        requirements=[{"primary_mpn": "LM358N", "target_qty": 100}],
    )
    assert req.name == "Test RFQ Import"
    assert len(req.requirements) == 1


def test_free_text_save_rfq_request_empty_items():
    """ApplyFreeformRfqRequest rejects empty requirements list."""
    from pydantic import ValidationError

    from app.schemas.ai import ApplyFreeformRfqRequest

    with pytest.raises(ValidationError):
        ApplyFreeformRfqRequest(name="Test", requirements=[])


def test_free_text_save_offers_request_valid():
    """SaveFreeformOffersRequest accepts valid payload."""
    from app.schemas.ai import SaveFreeformOffersRequest

    req = SaveFreeformOffersRequest(
        requisition_id=1,
        offers=[{"vendor_name": "Parts Direct", "mpn": "STM32F103", "qty_available": 500, "unit_price": 2.50}],
    )
    assert req.requisition_id == 1
    assert len(req.offers) == 1


def test_free_text_save_offers_bad_req_id():
    """SaveFreeformOffersRequest rejects non-positive requisition_id."""
    from pydantic import ValidationError

    from app.schemas.ai import SaveFreeformOffersRequest

    with pytest.raises(ValidationError):
        SaveFreeformOffersRequest(
            requisition_id=0,
            offers=[{"vendor_name": "Test", "mpn": "X"}],
        )


# ---------------------------------------------------------------------------
# Router endpoint tests
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


def test_parse_free_text_endpoint_disabled(ft_client):
    """POST /api/ai/parse-free-text with AI off returns 403."""
    with patch("app.routers.ai._ai_enabled", return_value=False):
        resp = ft_client.post("/api/ai/parse-free-text", json={"text": "LM358N x100"})
    assert resp.status_code == 403


def test_parse_free_text_endpoint_success(ft_client):
    """POST /api/ai/parse-free-text returns structured data."""
    mock_result = {
        "document_type": "rfq",
        "confidence": 0.92,
        "company_name": "Acme",
        "contact_name": "John",
        "contact_email": "john@acme.com",
        "notes": "Urgent",
        "line_items": [
            {"mpn": "LM358N", "quantity": 100, "target_price": 0.50},
        ],
    }

    with patch(
        "app.services.free_text_parser.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = ft_client.post(
            "/api/ai/parse-free-text",
            json={"text": "Need LM358N x100 at $0.50"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is True
    assert data["document_type"] == "rfq"
    assert len(data["line_items"]) == 1
    assert data["line_items"][0]["mpn"] == "LM358N"


def test_parse_free_text_endpoint_no_parts(ft_client):
    """POST /api/ai/parse-free-text with no extractable parts."""
    mock_result = {
        "document_type": "rfq",
        "confidence": 0.1,
        "line_items": [],
    }

    with patch(
        "app.services.free_text_parser.claude_structured",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = ft_client.post(
            "/api/ai/parse-free-text",
            json={"text": "Hello, how are you?"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is False


def test_save_free_text_rfq(ft_client, db_session):
    """POST /api/ai/save-free-text-rfq creates requisition + requirements."""
    resp = ft_client.post(
        "/api/ai/save-free-text-rfq",
        json={
            "name": "Acme RFQ",
            "customer_name": "Acme Corp",
            "line_items": [
                {"mpn": "LM358N", "quantity": 100, "target_price": 0.50},
                {"mpn": "NE555P", "quantity": 200},
            ],
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["requirements_created"] == 2
    assert data["requisition_name"] == "Acme RFQ"

    from app.models import Requirement, Requisition

    req = db_session.query(Requisition).filter(Requisition.id == data["requisition_id"]).first()
    assert req is not None
    assert req.name == "Acme RFQ"
    assert req.customer_name == "Acme Corp"
    assert req.status == "draft"

    reqs = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).all()
    assert len(reqs) == 2
    mpns = {r.primary_mpn for r in reqs}
    assert "LM358N" in mpns
    assert "NE555P" in mpns


def test_save_free_text_rfq_empty_items(ft_client):
    """POST /api/ai/save-free-text-rfq rejects empty line_items."""
    resp = ft_client.post(
        "/api/ai/save-free-text-rfq",
        json={"name": "Test", "line_items": []},
    )
    assert resp.status_code == 422


def test_save_free_text_offers(ft_client, db_session):
    """POST /api/ai/save-free-text-offers creates offers on existing requisition."""
    from app.models import Requisition

    req = Requisition(name="Test Req", created_by=1, status="active")
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)

    with patch("app.routers.ai.normalize_vendor_name", return_value="parts direct"):
        resp = ft_client.post(
            "/api/ai/save-free-text-offers",
            json={
                "requisition_id": req.id,
                "vendor_name": "Parts Direct",
                "line_items": [
                    {"mpn": "STM32F103", "quantity": 1000, "target_price": 2.50},
                ],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["offers_created"] == 1

    from app.models import Offer

    offers = db_session.query(Offer).filter(Offer.requisition_id == req.id).all()
    assert len(offers) == 1
    assert offers[0].mpn == "STM32F103"
    assert float(offers[0].unit_price) == 2.50
    assert offers[0].source == "free_text"


def test_save_free_text_offers_missing_req(ft_client):
    """POST /api/ai/save-free-text-offers with bad requisition_id returns 404."""
    resp = ft_client.post(
        "/api/ai/save-free-text-offers",
        json={
            "requisition_id": 99999,
            "vendor_name": "Test",
            "line_items": [{"mpn": "X", "quantity": 1}],
        },
    )
    assert resp.status_code == 404

"""
test_free_text_parser.py — Tests for legacy free-text parsing helpers and current freeform schemas.

Keeps coverage on the standalone parse_free_text helper module while validating
the request models used by the current split freeform RFQ / offer routes.

Covers: app.services.free_text_parser, app.schemas.ai freeform request models
"""

from unittest.mock import AsyncMock, patch

import pytest  # noqa: I001
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
# Schema validation tests for current freeform flow
# ---------------------------------------------------------------------------


def test_parse_freeform_rfq_request_requires_text():
    """ParseFreeformRfqRequest requires non-empty text."""
    from pydantic import ValidationError

    from app.schemas.ai import ParseFreeformRfqRequest

    with pytest.raises(ValidationError):
        ParseFreeformRfqRequest(raw_text="")


def test_parse_freeform_rfq_request_valid():
    """ParseFreeformRfqRequest accepts valid text."""
    from app.schemas.ai import ParseFreeformRfqRequest

    req = ParseFreeformRfqRequest(raw_text="LM358N x100")
    assert req.raw_text == "LM358N x100"


def test_parse_freeform_offer_request_valid():
    """ParseFreeformOfferRequest accepts valid text with requisition context."""
    from app.schemas.ai import ParseFreeformOfferRequest

    req = ParseFreeformOfferRequest(raw_text="We have LM358N x100", requisition_id=11)
    assert req.raw_text == "We have LM358N x100"
    assert req.requisition_id == 11


def test_apply_freeform_rfq_request_valid():
    """ApplyFreeformRfqRequest accepts valid payload."""
    from app.schemas.ai import ApplyFreeformRfqRequest

    req = ApplyFreeformRfqRequest(
        name="Test RFQ",
        customer_site_id=5,
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
        ApplyFreeformRfqRequest(name="Test", customer_site_id=5, requirements=[])


def test_save_freeform_offers_request_valid():
    """SaveFreeformOffersRequest accepts valid payload."""
    from app.schemas.ai import SaveFreeformOffersRequest

    req = SaveFreeformOffersRequest(
        requisition_id=1,
        offers=[{"vendor_name": "Parts Direct", "mpn": "STM32F103", "qty_available": 500, "unit_price": 2.50}],
    )
    assert req.requisition_id == 1
    assert req.offers[0].vendor_name == "Parts Direct"


def test_save_freeform_offers_bad_req_id():
    """SaveFreeformOffersRequest rejects non-positive requisition_id."""
    from pydantic import ValidationError

    from app.schemas.ai import SaveFreeformOffersRequest

    with pytest.raises(ValidationError):
        SaveFreeformOffersRequest(
            requisition_id=0,
            offers=[{"vendor_name": "Test", "mpn": "X"}],
        )

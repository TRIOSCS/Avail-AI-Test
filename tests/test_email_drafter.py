"""Tests for AI Email Drafter — mocked Gradient calls.

Covers: single part, multi-part, target price handling, date code/condition
        requirements, vendor contact name, buyer name, delivery deadline,
        subject generation, prompt construction, error handling, endpoint.
"""

import json
import os

os.environ["TESTING"] = "1"
os.environ["DO_GRADIENT_API_KEY"] = "test-key"

import pytest
from unittest.mock import AsyncMock, patch

from app.services.ai_email_drafter import draft_rfq_email, _format_parts


# ── Helpers ───────────────────────────────────────────────────────────


def _draft_response(subject="RFQ: LM358DR", body="We are looking to source..."):
    """Build a mock draft result from the LLM."""
    return {"subject": subject, "body": body}


def _part(part_number="LM358DR", manufacturer=None, quantity=1000,
          target_price=None, date_code_requirement=None,
          condition_requirement=None, delivery_deadline=None,
          additional_notes=None):
    """Build a part dict for draft requests."""
    return {
        "part_number": part_number,
        "manufacturer": manufacturer,
        "quantity": quantity,
        "target_price": target_price,
        "date_code_requirement": date_code_requirement,
        "condition_requirement": condition_requirement,
        "delivery_deadline": delivery_deadline,
        "additional_notes": additional_notes,
    }


# ── draft_rfq_email tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_draft_single_part():
    """Generates a draft for a single part."""
    mock_result = _draft_response(
        subject="RFQ: LM358DR",
        body="We are interested in sourcing LM358DR. Please provide pricing and availability.",
    )

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await draft_rfq_email(
            vendor_name="Arrow Electronics",
            parts=[_part("LM358DR", manufacturer="Texas Instruments", quantity=5000)],
            buyer_name="Mike",
        )

    assert result is not None
    assert result["subject"] == "RFQ: LM358DR"
    assert "LM358DR" in result["body"]
    assert "Mike" in result["body"]  # sign-off


@pytest.mark.asyncio
async def test_draft_multi_part():
    """Generates a draft with multiple parts."""
    parts = [
        _part("LM358DR", quantity=5000),
        _part("STM32F407VGT6", manufacturer="STMicroelectronics", quantity=200),
        _part("NE555P", quantity=10000),
    ]
    mock_result = _draft_response(
        subject="RFQ: LM358DR, STM32F407VGT6, NE555P",
        body="Please quote the following parts.",
    )

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await draft_rfq_email(
            vendor_name="Mouser",
            parts=parts,
            buyer_name="Mike",
        )

    assert result is not None
    assert "STM32F407VGT6" in result["subject"]

    # Verify prompt includes all parts
    call_args = mock.call_args
    prompt = call_args.args[0]
    assert "LM358DR" in prompt
    assert "STM32F407VGT6" in prompt
    assert "NE555P" in prompt


@pytest.mark.asyncio
async def test_draft_with_target_price():
    """Target price appears in the prompt."""
    mock_result = _draft_response()

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        await draft_rfq_email(
            vendor_name="Arrow",
            parts=[_part("LM358DR", target_price=0.45)],
            buyer_name="Mike",
        )

    prompt = mock.call_args.args[0]
    assert "0.45" in prompt


@pytest.mark.asyncio
async def test_draft_with_requirements():
    """Date code and condition requirements appear in prompt."""
    mock_result = _draft_response()

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        await draft_rfq_email(
            vendor_name="Arrow",
            parts=[_part(
                "LM358DR",
                date_code_requirement="2024+",
                condition_requirement="new only",
            )],
            buyer_name="Mike",
        )

    prompt = mock.call_args.args[0]
    assert "2024+" in prompt
    assert "new only" in prompt


@pytest.mark.asyncio
async def test_draft_with_contact_name():
    """Greeting uses vendor contact name when provided."""
    mock_result = _draft_response(body="Please quote the following.")

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await draft_rfq_email(
            vendor_name="Arrow",
            parts=[_part()],
            buyer_name="Mike",
            vendor_contact_name="John",
        )

    assert result["body"].startswith("Hi John,")


@pytest.mark.asyncio
async def test_draft_without_contact_name():
    """Greeting uses generic 'Hello' when no contact name."""
    mock_result = _draft_response(body="Please quote the following.")

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await draft_rfq_email(
            vendor_name="Arrow",
            parts=[_part()],
            buyer_name="Mike",
        )

    assert result["body"].startswith("Hello,")


@pytest.mark.asyncio
async def test_draft_default_subject():
    """Generates default subject when LLM returns empty subject."""
    mock_result = {"subject": "", "body": "Please quote."}

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await draft_rfq_email(
            vendor_name="Arrow",
            parts=[_part("LM358DR"), _part("NE555P")],
            buyer_name="Mike",
        )

    assert "LM358DR" in result["subject"]
    assert "NE555P" in result["subject"]


@pytest.mark.asyncio
async def test_draft_default_subject_many_parts():
    """Default subject truncates when more than 3 parts."""
    parts = [_part(f"PART-{i}") for i in range(5)]
    mock_result = {"subject": "", "body": "Please quote."}

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await draft_rfq_email(
            vendor_name="Arrow", parts=parts, buyer_name="Mike",
        )

    assert "+2 more" in result["subject"]


@pytest.mark.asyncio
async def test_draft_gradient_failure():
    """Returns None when Gradient API fails."""
    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = None
        result = await draft_rfq_email(
            vendor_name="Arrow",
            parts=[_part()],
            buyer_name="Mike",
        )

    assert result is None


@pytest.mark.asyncio
async def test_draft_empty_body():
    """Returns None when LLM returns empty body."""
    mock_result = {"subject": "RFQ", "body": ""}

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await draft_rfq_email(
            vendor_name="Arrow",
            parts=[_part()],
            buyer_name="Mike",
        )

    assert result is None


@pytest.mark.asyncio
async def test_draft_empty_parts():
    """Returns None for empty parts list."""
    result = await draft_rfq_email(
        vendor_name="Arrow", parts=[], buyer_name="Mike",
    )
    assert result is None


@pytest.mark.asyncio
async def test_draft_uses_generation_temperature():
    """Uses higher temperature (0.6) for natural-sounding generation."""
    mock_result = _draft_response()

    with patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        await draft_rfq_email(
            vendor_name="Arrow", parts=[_part()], buyer_name="Mike",
        )

    assert mock.call_args.kwargs["temperature"] == 0.6


# ── _format_parts unit tests ─────────────────────────────────────────


def test_format_parts_basic():
    parts = [_part("LM358DR", manufacturer="TI", quantity=5000)]
    result = _format_parts(parts)
    assert "LM358DR" in result
    assert "TI" in result
    assert "5000" in result


def test_format_parts_with_target_price():
    parts = [_part("LM358DR", target_price=0.45)]
    result = _format_parts(parts)
    assert "$0.45" in result


def test_format_parts_with_date_code():
    parts = [_part("LM358DR", date_code_requirement="2024+")]
    result = _format_parts(parts)
    assert "2024+" in result


def test_format_parts_with_condition():
    parts = [_part("LM358DR", condition_requirement="new only")]
    result = _format_parts(parts)
    assert "new only" in result


def test_format_parts_with_deadline():
    from datetime import date
    parts = [_part("LM358DR", delivery_deadline=date(2026, 3, 15))]
    result = _format_parts(parts)
    assert "2026-03-15" in result


def test_format_parts_caps_at_20():
    parts = [_part(f"PART-{i:03d}") for i in range(30)]
    result = _format_parts(parts)
    assert "PART-019" in result
    assert "PART-020" not in result


# ── Endpoint tests ───────────────────────────────────────────────────


def test_draft_rfq_email_endpoint(client):
    """POST /api/ai/draft-rfq-email returns subject and body."""
    mock_result = _draft_response(
        subject="RFQ: LM358DR",
        body="Please provide pricing for LM358DR.",
    )

    with patch("app.routers.ai.settings") as mock_settings, \
         patch("app.services.ai_email_drafter.gradient_json", new_callable=AsyncMock) as mock:
        mock_settings.ai_features_enabled = "all"
        mock.return_value = mock_result
        resp = client.post(
            "/api/ai/draft-rfq-email",
            json={
                "vendor_name": "Arrow Electronics",
                "buyer_name": "Mike",
                "parts": [
                    {"part_number": "LM358DR", "quantity": 5000},
                ],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert "RFQ" in data["subject"]
    assert "LM358DR" in data["body"]


def test_draft_rfq_email_endpoint_empty_parts(client):
    """Rejects empty parts list."""
    with patch("app.routers.ai.settings") as mock_settings:
        mock_settings.ai_features_enabled = "all"
        resp = client.post(
            "/api/ai/draft-rfq-email",
            json={
                "vendor_name": "Arrow",
                "buyer_name": "Mike",
                "parts": [],
            },
        )
    assert resp.status_code == 422


def test_draft_rfq_email_endpoint_ai_disabled(client):
    """Returns 403 when AI features are disabled."""
    with patch("app.routers.ai.settings") as mock_settings:
        mock_settings.ai_features_enabled = "off"
        resp = client.post(
            "/api/ai/draft-rfq-email",
            json={
                "vendor_name": "Arrow",
                "buyer_name": "Mike",
                "parts": [{"part_number": "LM358DR", "quantity": 1000}],
            },
        )
    assert resp.status_code == 403


def test_draft_rfq_email_endpoint_missing_buyer(client):
    """Rejects request without buyer_name."""
    with patch("app.routers.ai.settings") as mock_settings:
        mock_settings.ai_features_enabled = "all"
        resp = client.post(
            "/api/ai/draft-rfq-email",
            json={
                "vendor_name": "Arrow",
                "buyer_name": "",
                "parts": [{"part_number": "LM358DR", "quantity": 1000}],
            },
        )
    assert resp.status_code == 422

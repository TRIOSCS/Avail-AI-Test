"""Tests for AI Email Parser — mocked Gradient calls, realistic email samples.

Covers: single-part quotes, multi-part quotes, no-stock responses,
        multi-currency, partial quotes, price-on-request, edge cases,
        normalization, confidence thresholds, and the HTTP endpoint.
"""

import json
import os

os.environ["TESTING"] = "1"
os.environ["DO_GRADIENT_API_KEY"] = "test-key"

import pytest
from unittest.mock import AsyncMock, patch

from app.services.ai_email_parser import (
    parse_email,
    should_auto_apply,
    should_flag_review,
    _clean_email_body,
    _normalize_quotes,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _gradient_response(parsed_dict):
    """Simulate gradient_json returning a parsed dict."""
    return parsed_dict


# ── Sample Emails ──────────────────────────────────────────────────────

SINGLE_QUOTE_EMAIL = """
Hi,

Thanks for your inquiry. We can offer the following:

Part: STM32F407VGT6
Manufacturer: STMicroelectronics
Qty Available: 500 pcs
Unit Price: $8.50/ea
Lead Time: 2-3 weeks
Date Code: 2024+
Condition: New
Packaging: Tape & Reel

Quote valid for 7 days.

Best regards,
John Smith
Arrow Electronics
"""

MULTI_PART_EMAIL = """
Hi Mike,

Please see our availability below:

1. LM358DR - TI - 10,000 pcs - $0.45/ea - In stock - New - T&R
2. STM32F407VGT6 - ST - 200 pcs - $12.50/ea - 4 weeks lead - New - Tray
3. NE555P - TI - No stock

Let me know if you'd like to proceed.

Regards,
Jane Doe
Mouser Electronics
"""

NO_STOCK_EMAIL = """
Hi,

Unfortunately we do not have stock of the following items:
- STM32F407VGT6
- LM358DR

We will keep your request on file and notify you if stock becomes available.

Best,
Supply Team
"""

MULTI_CURRENCY_EMAIL = """
Dear Sir,

We can quote as follows:

STM32F407VGT6: €9.20 per unit, 1000 pcs available, MOQ 100
LM358DR: ¥3.50 per unit, 50000 pcs, MOQ 1000, DC2339

Payment: T/T 30 days

Regards,
Beijing Components Ltd
"""

PRICE_ON_REQUEST_EMAIL = """
Thank you for your inquiry on the following:

MPN: XC7K325T-2FFG900I (Xilinx)

This is a specialty item. Please call for pricing.
We have 15 units available, new condition, DC2023+.

Contact: sales@vendor.com
"""

OOO_EMAIL = """
I am out of the office until February 28, 2026.
For urgent matters, please contact my colleague at backup@vendor.com.

Best regards,
Tom
"""

HTML_EMAIL = """
<html><body>
<p>Hi Mike,</p>
<table>
<tr><th>Part</th><th>Qty</th><th>Price</th></tr>
<tr><td>LM358DR</td><td>5000</td><td>$0.38</td></tr>
</table>
<p>Thanks,<br>Vendor</p>
<p style="font-size:8px">DISCLAIMER: This email is confidential...</p>
</body></html>
"""


# ── parse_email tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_single_quote():
    """Parses a single-part quote with all fields."""
    mock_result = {
        "quotes": [
            {
                "part_number": "STM32F407VGT6",
                "manufacturer": "STMicroelectronics",
                "quantity_available": 500,
                "unit_price": 8.50,
                "currency": "USD",
                "lead_time_days": 17,
                "lead_time_text": "2-3 weeks",
                "moq": None,
                "date_code": "2024+",
                "condition": "new",
                "packaging": "tape & reel",
                "notes": "Quote valid for 7 days",
                "confidence": 0.95,
            }
        ],
        "overall_confidence": 0.95,
        "email_type": "quote",
        "vendor_notes": None,
    }

    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await parse_email(SINGLE_QUOTE_EMAIL, "RE: RFQ STM32F407VGT6", "Arrow Electronics")

    assert result is not None
    assert len(result["quotes"]) == 1
    q = result["quotes"][0]
    assert q["part_number"] == "STM32F407VGT6"
    assert q["unit_price"] == 8.50
    assert q["currency"] == "USD"
    assert result["overall_confidence"] == 0.95


@pytest.mark.asyncio
async def test_parse_multi_part_quote():
    """Parses email with multiple line items including no-stock."""
    mock_result = {
        "quotes": [
            {
                "part_number": "LM358DR",
                "manufacturer": "TI",
                "quantity_available": 10000,
                "unit_price": 0.45,
                "currency": "USD",
                "condition": "new",
                "packaging": "tape & reel",
                "confidence": 0.9,
            },
            {
                "part_number": "STM32F407VGT6",
                "manufacturer": "ST",
                "quantity_available": 200,
                "unit_price": 12.50,
                "currency": "USD",
                "lead_time_text": "4 weeks",
                "condition": "new",
                "packaging": "tray",
                "confidence": 0.9,
            },
            {
                "part_number": "NE555P",
                "manufacturer": "TI",
                "quantity_available": 0,
                "unit_price": None,
                "notes": "No stock",
                "confidence": 0.85,
            },
        ],
        "overall_confidence": 0.88,
        "email_type": "partial",
        "vendor_notes": None,
    }

    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await parse_email(MULTI_PART_EMAIL, "RE: RFQ", "Mouser")

    assert result is not None
    assert len(result["quotes"]) == 3
    assert result["quotes"][0]["part_number"] == "LM358DR"
    assert result["quotes"][2]["quantity_available"] is None  # 0 normalizes to None
    assert result["quotes"][2]["unit_price"] is None


@pytest.mark.asyncio
async def test_parse_no_stock():
    """Parses a no-stock response correctly."""
    mock_result = {
        "quotes": [
            {"part_number": "STM32F407VGT6", "quantity_available": 0, "unit_price": None, "confidence": 0.9},
            {"part_number": "LM358DR", "quantity_available": 0, "unit_price": None, "confidence": 0.9},
        ],
        "overall_confidence": 0.9,
        "email_type": "no_stock",
        "vendor_notes": "Will notify if stock becomes available",
    }

    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await parse_email(NO_STOCK_EMAIL)

    assert result["email_type"] == "no_stock"
    assert all(q["quantity_available"] is None for q in result["quotes"])  # 0 normalizes to None


@pytest.mark.asyncio
async def test_parse_multi_currency():
    """Parses quotes with EUR and CNY currencies."""
    mock_result = {
        "quotes": [
            {
                "part_number": "STM32F407VGT6",
                "quantity_available": 1000,
                "unit_price": 9.20,
                "currency": "EUR",
                "moq": 100,
                "confidence": 0.85,
            },
            {
                "part_number": "LM358DR",
                "quantity_available": 50000,
                "unit_price": 3.50,
                "currency": "CNY",
                "moq": 1000,
                "date_code": "2339",
                "confidence": 0.85,
            },
        ],
        "overall_confidence": 0.85,
        "email_type": "quote",
        "vendor_notes": "Payment: T/T 30 days",
    }

    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await parse_email(MULTI_CURRENCY_EMAIL, vendor_name="Beijing Components")

    assert result["quotes"][0]["currency"] == "EUR"
    assert result["quotes"][1]["currency"] == "CNY"


@pytest.mark.asyncio
async def test_parse_price_on_request():
    """Parses a price-on-request response."""
    mock_result = {
        "quotes": [
            {
                "part_number": "XC7K325T-2FFG900I",
                "manufacturer": "Xilinx",
                "quantity_available": 15,
                "unit_price": None,
                "condition": "new",
                "date_code": "2023+",
                "notes": "Call for pricing",
                "confidence": 0.7,
            },
        ],
        "overall_confidence": 0.7,
        "email_type": "price_on_request",
        "vendor_notes": None,
    }

    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await parse_email(PRICE_ON_REQUEST_EMAIL)

    assert result["email_type"] == "price_on_request"
    assert result["quotes"][0]["unit_price"] is None
    assert result["quotes"][0]["quantity_available"] == 15


@pytest.mark.asyncio
async def test_parse_ooo_bounce():
    """Recognizes out-of-office emails."""
    mock_result = {
        "quotes": [],
        "overall_confidence": 0.95,
        "email_type": "ooo_bounce",
        "vendor_notes": "Out of office until Feb 28. Contact backup@vendor.com",
    }

    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await parse_email(OOO_EMAIL)

    assert result["email_type"] == "ooo_bounce"
    assert len(result["quotes"]) == 0


@pytest.mark.asyncio
async def test_parse_empty_body():
    """Returns None for empty email body."""
    result = await parse_email("")
    assert result is None


@pytest.mark.asyncio
async def test_parse_gradient_failure():
    """Returns None when Gradient API fails."""
    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = None
        result = await parse_email("Some email content")

    assert result is None


# ── Confidence threshold tests ─────────────────────────────────────────


def test_auto_apply_high_confidence():
    assert should_auto_apply({"overall_confidence": 0.9}) is True


def test_auto_apply_low_confidence():
    assert should_auto_apply({"overall_confidence": 0.6}) is False


def test_flag_review_mid_confidence():
    assert should_flag_review({"overall_confidence": 0.65}) is True


def test_flag_review_high_confidence():
    assert should_flag_review({"overall_confidence": 0.85}) is False


def test_flag_review_low_confidence():
    assert should_flag_review({"overall_confidence": 0.3}) is False


# ── Normalization tests ────────────────────────────────────────────────


def test_normalize_quotes_price():
    """Normalizes price values."""
    result = {"quotes": [{"unit_price": "8.50", "currency": "USD"}]}
    _normalize_quotes(result)
    assert result["quotes"][0]["unit_price"] == 8.50


def test_normalize_quotes_quantity():
    """Normalizes quantity values."""
    result = {"quotes": [{"quantity_available": "10000"}]}
    _normalize_quotes(result)
    assert result["quotes"][0]["quantity_available"] == 10000


def test_normalize_quotes_missing_currency():
    """Defaults to USD when currency is missing."""
    result = {"quotes": [{"unit_price": 5.0}]}
    _normalize_quotes(result)
    assert result["quotes"][0]["currency"] == "USD"


def test_normalize_confidence_clamped():
    """Clamps confidence to 0-1 range."""
    result = {"quotes": [{"confidence": 1.5}]}
    _normalize_quotes(result)
    assert result["quotes"][0]["confidence"] == 1.0


# ── Email cleanup tests ───────────────────────────────────────────────


def test_clean_html():
    """Strips HTML tags."""
    cleaned = _clean_email_body("<p>Hello <b>world</b></p>")
    assert "<" not in cleaned
    assert "Hello" in cleaned


def test_clean_disclaimer():
    """Strips email disclaimers."""
    body = "Quote: $5.00\n\nDISCLAIMER: This email is confidential and blah blah"
    cleaned = _clean_email_body(body)
    assert "Quote" in cleaned


def test_clean_empty():
    assert _clean_email_body("") == ""
    assert _clean_email_body(None) == ""


def test_clean_preserves_newlines():
    """Newlines survive cleaning so tabular data stays intact."""
    body = "Part: LM358DR\nQty: 5000\nPrice: $0.45"
    cleaned = _clean_email_body(body)
    assert "\n" in cleaned
    assert "LM358DR" in cleaned


def test_clean_html_table_preserves_rows():
    """HTML table rows become separate lines."""
    cleaned = _clean_email_body(HTML_EMAIL)
    assert "LM358DR" in cleaned
    assert "5000" in cleaned
    # <tr> → newline ensures rows are on separate lines
    lines = [l.strip() for l in cleaned.split("\n") if l.strip()]
    assert len(lines) > 1


def test_clean_collapses_excessive_blank_lines():
    """3+ consecutive newlines collapse to 2."""
    body = "Line 1\n\n\n\n\nLine 2"
    cleaned = _clean_email_body(body)
    assert "\n\n\n" not in cleaned
    assert "Line 1" in cleaned
    assert "Line 2" in cleaned


# ── Parse edge cases ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_html_table_email():
    """Parses an HTML table email (tags stripped, structure preserved)."""
    mock_result = {
        "quotes": [
            {
                "part_number": "LM358DR",
                "quantity_available": 5000,
                "unit_price": 0.38,
                "currency": "USD",
                "confidence": 0.85,
            }
        ],
        "overall_confidence": 0.85,
        "email_type": "quote",
        "vendor_notes": None,
    }

    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await parse_email(HTML_EMAIL, vendor_name="Test Vendor")

    assert result is not None
    assert len(result["quotes"]) == 1
    assert result["quotes"][0]["part_number"] == "LM358DR"

    # Verify the prompt sent to gradient_json has table content intact
    call_args = mock.call_args
    prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
    assert "LM358DR" in prompt
    assert "5000" in prompt


@pytest.mark.asyncio
async def test_parse_long_email_truncation():
    """Very long emails are truncated to avoid token waste."""
    long_body = "Part: LM358DR, $0.45\n" * 500  # ~10K chars

    mock_result = {
        "quotes": [{"part_number": "LM358DR", "unit_price": 0.45, "confidence": 0.8}],
        "overall_confidence": 0.8,
        "email_type": "quote",
        "vendor_notes": None,
    }

    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await parse_email(long_body)

    assert result is not None
    # Verify truncation: prompt should be under 5000 chars of body
    call_args = mock.call_args
    prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
    assert len(prompt) < 6000  # 5000 body + subject/vendor header


@pytest.mark.asyncio
async def test_parse_non_dict_response():
    """Returns None when gradient returns a list instead of dict."""
    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = [{"part_number": "LM358DR"}]
        result = await parse_email("Some email")

    assert result is None


@pytest.mark.asyncio
async def test_parse_quotes_as_string():
    """Handles edge case where quotes field is a JSON string instead of list."""
    import json as _json
    mock_result = {
        "quotes": _json.dumps([{"part_number": "LM358DR", "confidence": 0.9}]),
        "overall_confidence": 0.8,
        "email_type": "quote",
        "vendor_notes": None,
    }

    with patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await parse_email("LM358DR $0.45/ea")

    assert result is not None
    assert isinstance(result["quotes"], list)
    assert result["quotes"][0]["part_number"] == "LM358DR"


# ── Endpoint test ──────────────────────────────────────────────────────


def test_parse_email_endpoint(client):
    """POST /api/ai/parse-email returns parsed quotes."""
    mock_result = {
        "quotes": [
            {
                "part_number": "LM358DR",
                "unit_price": 0.45,
                "quantity_available": 5000,
                "currency": "USD",
                "confidence": 0.9,
            }
        ],
        "overall_confidence": 0.9,
        "email_type": "quote",
        "vendor_notes": None,
    }

    with patch("app.routers.ai.settings") as mock_settings, \
         patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock_settings.ai_features_enabled = "all"
        mock.return_value = mock_result
        resp = client.post(
            "/api/ai/parse-email",
            json={
                "email_body": "LM358DR - $0.45/ea - 5000 pcs available",
                "email_subject": "RE: RFQ LM358DR",
                "vendor_name": "Test Vendor",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is True
    assert len(data["quotes"]) == 1
    assert data["quotes"][0]["part_number"] == "LM358DR"
    assert data["auto_apply"] is True


def test_parse_email_endpoint_empty_body(client):
    """Rejects empty email body."""
    with patch("app.routers.ai.settings") as mock_settings:
        mock_settings.ai_features_enabled = "all"
        resp = client.post(
            "/api/ai/parse-email",
            json={"email_body": ""},
        )
    assert resp.status_code == 422  # Pydantic validation error


def test_parse_email_endpoint_failure(client):
    """Returns parsed=False when parser fails."""
    with patch("app.routers.ai.settings") as mock_settings, \
         patch("app.services.ai_email_parser.gradient_json", new_callable=AsyncMock) as mock:
        mock_settings.ai_features_enabled = "all"
        mock.return_value = None
        resp = client.post(
            "/api/ai/parse-email",
            json={"email_body": "Some email text"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["parsed"] is False

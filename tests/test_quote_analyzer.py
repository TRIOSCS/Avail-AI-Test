"""Tests for AI Quote Analyzer — mocked Gradient calls.

Covers: basic comparison, single quote rejection, anomaly detection,
        vendor scoring, MOQ analysis, prompt construction, result validation,
        error handling, and the HTTP endpoint.
"""

import os

os.environ["TESTING"] = "1"
os.environ["DO_GRADIENT_API_KEY"] = "test-key"

import pytest
from unittest.mock import AsyncMock, patch

from app.services.ai_quote_analyzer import compare_quotes, _format_quotes, _validate_result


# ── Helpers ───────────────────────────────────────────────────────────


def _quote(vendor_name="Arrow", unit_price=1.50, vendor_score=85,
           currency="USD", quantity_available=5000, lead_time_days=14,
           date_code="2025+", condition="New", moq=100):
    """Build a quote dict for comparison requests."""
    return {
        "vendor_name": vendor_name,
        "vendor_score": vendor_score,
        "unit_price": unit_price,
        "currency": currency,
        "quantity_available": quantity_available,
        "lead_time_days": lead_time_days,
        "date_code": date_code,
        "condition": condition,
        "moq": moq,
    }


def _comparison_result(summary="Three quotes received.",
                       recommendation="Go with Arrow for best overall value.",
                       risk_factors=None, anomalies=None):
    """Build a mock comparison result from the LLM."""
    return {
        "summary": summary,
        "recommendation": recommendation,
        "risk_factors": risk_factors or [],
        "best_price": {"vendor": "Mouser", "unit_price": 1.20, "reason": "Lowest price"},
        "fastest_delivery": {"vendor": "Arrow", "lead_time_days": 7, "reason": "Fastest"},
        "best_overall": {"vendor": "Arrow", "reason": "Best balance of price and reliability"},
        "anomalies": anomalies or [],
    }


# ── compare_quotes tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compare_basic():
    """Compares two quotes and returns structured result."""
    quotes = [
        _quote("Arrow", unit_price=1.50, lead_time_days=7),
        _quote("Mouser", unit_price=1.20, lead_time_days=14),
    ]
    mock_result = _comparison_result()

    with patch("app.services.ai_quote_analyzer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await compare_quotes("LM358DR", quotes)

    assert result is not None
    assert result["summary"] == "Three quotes received."
    assert result["recommendation"] is not None
    assert result["best_price"]["vendor"] == "Mouser"
    assert result["best_overall"]["vendor"] == "Arrow"
    assert result["quote_count"] == 2


@pytest.mark.asyncio
async def test_compare_three_vendors():
    """Handles three-way comparison."""
    quotes = [
        _quote("Arrow", unit_price=1.50),
        _quote("Mouser", unit_price=1.20),
        _quote("DigiKey", unit_price=1.35, vendor_score=90),
    ]
    mock_result = _comparison_result()

    with patch("app.services.ai_quote_analyzer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await compare_quotes("LM358DR", quotes)

    assert result is not None
    assert result["quote_count"] == 3

    # Verify all vendors appear in prompt
    prompt = mock.call_args.args[0]
    assert "Arrow" in prompt
    assert "Mouser" in prompt
    assert "DigiKey" in prompt


@pytest.mark.asyncio
async def test_compare_with_required_qty():
    """Required quantity appears in the prompt."""
    quotes = [
        _quote("Arrow", unit_price=1.50),
        _quote("Mouser", unit_price=1.20),
    ]
    mock_result = _comparison_result()

    with patch("app.services.ai_quote_analyzer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        await compare_quotes("LM358DR", quotes, required_qty=1000)

    prompt = mock.call_args.args[0]
    assert "1000" in prompt
    assert "Required quantity" in prompt


@pytest.mark.asyncio
async def test_compare_single_quote_returns_none():
    """Returns None when only one quote (nothing to compare)."""
    result = await compare_quotes("LM358DR", [_quote()])
    assert result is None


@pytest.mark.asyncio
async def test_compare_empty_quotes_returns_none():
    """Returns None for empty quotes list."""
    result = await compare_quotes("LM358DR", [])
    assert result is None


@pytest.mark.asyncio
async def test_compare_with_anomalies():
    """Passes through anomaly flags from LLM."""
    quotes = [
        _quote("Arrow", unit_price=1.50),
        _quote("ShadyVendor", unit_price=0.10, vendor_score=20),
    ]
    mock_result = _comparison_result(
        anomalies=["ShadyVendor price 93% below median — possible counterfeit risk"],
        risk_factors=["Low vendor reliability score (20/100)"],
    )

    with patch("app.services.ai_quote_analyzer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        result = await compare_quotes("LM358DR", quotes)

    assert len(result["anomalies"]) == 1
    assert "counterfeit" in result["anomalies"][0].lower()
    assert len(result["risk_factors"]) == 1


@pytest.mark.asyncio
async def test_compare_gradient_failure():
    """Returns None when Gradient API fails."""
    quotes = [_quote("Arrow"), _quote("Mouser")]

    with patch("app.services.ai_quote_analyzer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = None
        result = await compare_quotes("LM358DR", quotes)

    assert result is None


@pytest.mark.asyncio
async def test_compare_uses_low_temperature():
    """Uses temperature 0.2 for consistent analytical output."""
    quotes = [_quote("Arrow"), _quote("Mouser")]
    mock_result = _comparison_result()

    with patch("app.services.ai_quote_analyzer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        await compare_quotes("LM358DR", quotes)

    assert mock.call_args.kwargs["temperature"] == 0.2


@pytest.mark.asyncio
async def test_compare_part_number_in_prompt():
    """Part number appears in the prompt."""
    quotes = [_quote("Arrow"), _quote("Mouser")]
    mock_result = _comparison_result()

    with patch("app.services.ai_quote_analyzer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        await compare_quotes("STM32F407VGT6", quotes)

    prompt = mock.call_args.args[0]
    assert "STM32F407VGT6" in prompt


# ── _format_quotes tests ─────────────────────────────────────────────


def test_format_quotes_basic():
    quotes = [_quote("Arrow", unit_price=1.50, vendor_score=85)]
    result = _format_quotes(quotes)
    assert "Arrow" in result
    assert "85/100" in result
    assert "1.5000" in result


def test_format_quotes_with_all_fields():
    quotes = [_quote("Arrow", unit_price=1.50, lead_time_days=7,
                     date_code="2025+", condition="New", moq=100)]
    result = _format_quotes(quotes)
    assert "7 day lead" in result
    assert "DC: 2025+" in result
    assert "New" in result
    assert "MOQ: 100" in result


def test_format_quotes_no_price():
    quotes = [_quote("Arrow", unit_price=None)]
    result = _format_quotes(quotes)
    assert "Arrow" in result
    assert "USD" not in result  # No price line


def test_format_quotes_caps_at_15():
    quotes = [_quote(f"Vendor-{i}") for i in range(20)]
    result = _format_quotes(quotes)
    assert "Vendor-14" in result
    assert "Vendor-15" not in result


def test_format_quotes_no_vendor_score():
    quotes = [_quote("Arrow", vendor_score=None)]
    result = _format_quotes(quotes)
    assert "reliability" not in result


# ── _validate_result tests ───────────────────────────────────────────


def test_validate_result_good():
    raw = _comparison_result()
    quotes = [_quote("Arrow"), _quote("Mouser")]
    result = _validate_result(raw, quotes)
    assert result["summary"] == "Three quotes received."
    assert result["quote_count"] == 2
    assert isinstance(result["risk_factors"], list)
    assert isinstance(result["anomalies"], list)


def test_validate_result_non_list_risk_factors():
    raw = _comparison_result()
    raw["risk_factors"] = "not a list"
    result = _validate_result(raw, [_quote(), _quote()])
    assert result["risk_factors"] == []


def test_validate_result_non_list_anomalies():
    raw = _comparison_result()
    raw["anomalies"] = "not a list"
    result = _validate_result(raw, [_quote(), _quote()])
    assert result["anomalies"] == []


def test_validate_result_non_dict_best_price():
    raw = _comparison_result()
    raw["best_price"] = "Arrow"
    result = _validate_result(raw, [_quote(), _quote()])
    assert result["best_price"] is None


def test_validate_result_missing_fields():
    raw = {"summary": "Brief comparison."}
    result = _validate_result(raw, [_quote(), _quote()])
    assert result["summary"] == "Brief comparison."
    assert result["recommendation"] == ""
    assert result["risk_factors"] == []
    assert result["best_price"] is None


# ── Endpoint tests ───────────────────────────────────────────────────


def test_compare_quotes_endpoint(client):
    """POST /api/ai/compare-quotes returns comparison result."""
    mock_result = _comparison_result()

    with patch("app.routers.ai.settings") as mock_settings, \
         patch("app.services.ai_quote_analyzer.gradient_json", new_callable=AsyncMock) as mock:
        mock_settings.ai_features_enabled = "all"
        mock.return_value = mock_result
        resp = client.post(
            "/api/ai/compare-quotes",
            json={
                "part_number": "LM358DR",
                "quotes": [
                    {"vendor_name": "Arrow", "unit_price": 1.50, "quantity_available": 5000},
                    {"vendor_name": "Mouser", "unit_price": 1.20, "quantity_available": 3000},
                ],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert "summary" in data
    assert "recommendation" in data
    assert "best_overall" in data


def test_compare_quotes_endpoint_single_quote(client):
    """Rejects request with only one quote."""
    with patch("app.routers.ai.settings") as mock_settings:
        mock_settings.ai_features_enabled = "all"
        resp = client.post(
            "/api/ai/compare-quotes",
            json={
                "part_number": "LM358DR",
                "quotes": [
                    {"vendor_name": "Arrow", "unit_price": 1.50},
                ],
            },
        )
    assert resp.status_code == 422  # Pydantic min_length=2


def test_compare_quotes_endpoint_ai_disabled(client):
    """Returns 403 when AI features are disabled."""
    with patch("app.routers.ai.settings") as mock_settings:
        mock_settings.ai_features_enabled = "off"
        resp = client.post(
            "/api/ai/compare-quotes",
            json={
                "part_number": "LM358DR",
                "quotes": [
                    {"vendor_name": "Arrow", "unit_price": 1.50},
                    {"vendor_name": "Mouser", "unit_price": 1.20},
                ],
            },
        )
    assert resp.status_code == 403


def test_compare_quotes_endpoint_missing_part_number(client):
    """Rejects request without part_number."""
    with patch("app.routers.ai.settings") as mock_settings:
        mock_settings.ai_features_enabled = "all"
        resp = client.post(
            "/api/ai/compare-quotes",
            json={
                "part_number": "",
                "quotes": [
                    {"vendor_name": "Arrow", "unit_price": 1.50},
                    {"vendor_name": "Mouser", "unit_price": 1.20},
                ],
            },
        )
    assert resp.status_code == 422


def test_compare_quotes_endpoint_with_required_qty(client):
    """Accepts optional required_qty field."""
    mock_result = _comparison_result()

    with patch("app.routers.ai.settings") as mock_settings, \
         patch("app.services.ai_quote_analyzer.gradient_json", new_callable=AsyncMock) as mock:
        mock_settings.ai_features_enabled = "all"
        mock.return_value = mock_result
        resp = client.post(
            "/api/ai/compare-quotes",
            json={
                "part_number": "LM358DR",
                "required_qty": 1000,
                "quotes": [
                    {"vendor_name": "Arrow", "unit_price": 1.50, "quantity_available": 5000},
                    {"vendor_name": "Mouser", "unit_price": 1.20, "quantity_available": 3000},
                ],
            },
        )

    assert resp.status_code == 200
    assert resp.json()["available"] is True

"""Tests for AI Part Number Normalizer — mocked Gradient calls.

Covers: single part, batch normalization, cache hits, low confidence fallback,
        manufacturer inference, package suffix extraction, cross-reference
        detection, edge cases, and the HTTP endpoint.
"""

import json
import os

os.environ["TESTING"] = "1"
os.environ["DO_GRADIENT_API_KEY"] = "test-key"

import pytest
from unittest.mock import AsyncMock, patch

from app.services.ai_part_normalizer import (
    normalize_parts,
    clear_cache,
    _fallback,
    _validate_result,
    _cache,
    CONFIDENCE_THRESHOLD,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_cache():
    """Clear normalizer cache before each test."""
    _cache.clear()
    yield
    _cache.clear()


def _norm_result(original, normalized, manufacturer=None, base_part=None,
                 package_code=None, is_alias=False, confidence=0.9):
    """Build a single normalized part result dict."""
    return {
        "original": original,
        "normalized": normalized,
        "manufacturer": manufacturer,
        "base_part": base_part,
        "package_code": package_code,
        "is_alias": is_alias,
        "confidence": confidence,
    }


# ── Single part normalization ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_normalize_single_part():
    """Normalizes a single part number with manufacturer and package info."""
    mock_result = [
        _norm_result(
            "lm358dr", "LM358DR",
            manufacturer="Texas Instruments",
            base_part="LM358",
            package_code="DR",
            confidence=0.95,
        )
    ]

    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        results = await normalize_parts(["lm358dr"])

    assert len(results) == 1
    assert results[0]["normalized"] == "LM358DR"
    assert results[0]["manufacturer"] == "Texas Instruments"
    assert results[0]["base_part"] == "LM358"
    assert results[0]["package_code"] == "DR"
    assert results[0]["is_alias"] is False
    assert results[0]["confidence"] == 0.95


@pytest.mark.asyncio
async def test_normalize_stm32_part():
    """Normalizes a complex STM32 part number."""
    mock_result = [
        _norm_result(
            "stm32f407vgt6", "STM32F407VGT6",
            manufacturer="STMicroelectronics",
            base_part="STM32F407",
            package_code="VGT6",
            confidence=0.95,
        )
    ]

    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        results = await normalize_parts(["stm32f407vgt6"])

    assert results[0]["normalized"] == "STM32F407VGT6"
    assert results[0]["manufacturer"] == "STMicroelectronics"


# ── Batch normalization ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_normalize_batch():
    """Processes multiple parts in a single call."""
    parts = ["LM358DR", "NE555P", "STM32F407VGT6"]
    mock_result = [
        _norm_result("LM358DR", "LM358DR", manufacturer="Texas Instruments", confidence=0.95),
        _norm_result("NE555P", "NE555P", manufacturer="Texas Instruments", confidence=0.9),
        _norm_result("STM32F407VGT6", "STM32F407VGT6", manufacturer="STMicroelectronics", confidence=0.95),
    ]

    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        results = await normalize_parts(parts)

    assert len(results) == 3
    assert results[0]["manufacturer"] == "Texas Instruments"
    assert results[2]["manufacturer"] == "STMicroelectronics"
    mock.assert_called_once()  # Single LLM call for all 3 parts


# ── Cache behavior ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit():
    """Second call for the same part returns cached result without LLM call."""
    mock_result = [_norm_result("LM358DR", "LM358DR", confidence=0.9)]

    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        results1 = await normalize_parts(["LM358DR"])
        results2 = await normalize_parts(["LM358DR"])

    assert results1 == results2
    mock.assert_called_once()  # Only one LLM call


@pytest.mark.asyncio
async def test_partial_cache():
    """Mixed cached and uncached parts: only uncached parts trigger LLM call."""
    # First call: cache LM358DR
    mock1 = [_norm_result("LM358DR", "LM358DR", confidence=0.9)]
    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock1
        await normalize_parts(["LM358DR"])

    # Second call: LM358DR cached, NE555P uncached
    mock2 = [_norm_result("NE555P", "NE555P", confidence=0.9)]
    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock2
        results = await normalize_parts(["LM358DR", "NE555P"])

    assert len(results) == 2
    assert results[0]["normalized"] == "LM358DR"  # from cache
    assert results[1]["normalized"] == "NE555P"    # from LLM
    mock.assert_called_once()  # Only called for NE555P


@pytest.mark.asyncio
async def test_clear_cache():
    """clear_cache() empties the cache."""
    _cache["test"] = {"normalized": "TEST"}
    count = clear_cache()
    assert count == 1
    assert len(_cache) == 0


# ── Low confidence fallback ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_low_confidence_fallback():
    """Low confidence result returns original string unchanged."""
    mock_result = [
        _norm_result("XYZABC123", "XYZABC123", confidence=0.3)
    ]

    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        results = await normalize_parts(["XYZABC123"])

    assert results[0]["normalized"] == "XYZABC123"
    assert results[0]["manufacturer"] is None
    assert results[0]["confidence"] == 0.3


# ── Cross-reference / alias detection ────────────────────────────────


@pytest.mark.asyncio
async def test_alias_detection():
    """Detects distributor SKUs that aren't real MPNs."""
    mock_result = [
        _norm_result(
            "296-1395-1-ND", "LM358DR",
            manufacturer="Texas Instruments",
            is_alias=True,
            confidence=0.85,
        )
    ]

    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        results = await normalize_parts(["296-1395-1-ND"])

    assert results[0]["is_alias"] is True
    assert results[0]["normalized"] == "LM358DR"


# ── Part number variants ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_normalize_variant_formats():
    """Handles common part number variations (spaces, dashes, slashes)."""
    parts = ["LM358 DR", "LM358-DR", "LM358D/R"]
    mock_result = [
        _norm_result("LM358 DR", "LM358DR", base_part="LM358", package_code="DR", confidence=0.9),
        _norm_result("LM358-DR", "LM358DR", base_part="LM358", package_code="DR", confidence=0.9),
        _norm_result("LM358D/R", "LM358DR", base_part="LM358", package_code="DR", confidence=0.85),
    ]

    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        results = await normalize_parts(parts)

    # All variants normalize to the same canonical form
    assert all(r["normalized"] == "LM358DR" for r in results)


# ── Error handling ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gradient_failure_returns_fallbacks():
    """Returns fallback results when Gradient API fails."""
    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = None
        results = await normalize_parts(["LM358DR", "NE555P"])

    assert len(results) == 2
    assert results[0]["normalized"] == "LM358DR"
    assert results[0]["confidence"] == 0.0
    assert results[1]["normalized"] == "NE555P"
    assert results[1]["confidence"] == 0.0


@pytest.mark.asyncio
async def test_empty_input():
    """Empty list returns empty list."""
    results = await normalize_parts([])
    assert results == []


@pytest.mark.asyncio
async def test_dict_wrapped_response():
    """Handles LLM returning {parts: [...]} instead of bare array."""
    mock_result = {
        "parts": [_norm_result("LM358DR", "LM358DR", confidence=0.9)]
    }

    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        results = await normalize_parts(["LM358DR"])

    assert results[0]["normalized"] == "LM358DR"


@pytest.mark.asyncio
async def test_fewer_results_than_inputs():
    """Handles LLM returning fewer results than input parts."""
    mock_result = [
        _norm_result("LM358DR", "LM358DR", confidence=0.9),
        # Missing second result
    ]

    with patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock.return_value = mock_result
        results = await normalize_parts(["LM358DR", "NE555P"])

    assert len(results) == 2
    assert results[0]["normalized"] == "LM358DR"
    assert results[1]["confidence"] == 0.0  # fallback


# ── _validate_result unit tests ──────────────────────────────────────


def test_validate_result_good():
    parsed = {
        "normalized": "LM358DR",
        "manufacturer": "Texas Instruments",
        "base_part": "LM358",
        "package_code": "DR",
        "is_alias": False,
        "confidence": 0.9,
    }
    result = _validate_result("lm358dr", parsed)
    assert result["normalized"] == "LM358DR"
    assert result["manufacturer"] == "Texas Instruments"


def test_validate_result_low_confidence():
    parsed = {"normalized": "LM358DR", "confidence": 0.3}
    result = _validate_result("lm358dr", parsed)
    assert result["normalized"] == "LM358DR"  # fallback uppercase
    assert result["manufacturer"] is None


def test_validate_result_not_dict():
    result = _validate_result("LM358DR", "not a dict")
    assert result["confidence"] == 0.0


def test_validate_result_clamps_confidence():
    parsed = {"normalized": "LM358DR", "confidence": 1.5}
    result = _validate_result("lm358dr", parsed)
    assert result["confidence"] == 1.0


# ── _fallback unit tests ─────────────────────────────────────────────


def test_fallback():
    result = _fallback("  lm358dr  ")
    assert result["original"] == "  lm358dr  "
    assert result["normalized"] == "LM358DR"
    assert result["manufacturer"] is None
    assert result["confidence"] == 0.0


# ── Endpoint test ────────────────────────────────────────────────────


def test_normalize_parts_endpoint(client):
    """POST /api/ai/normalize-parts returns normalized results."""
    mock_result = [
        _norm_result("LM358DR", "LM358DR", manufacturer="Texas Instruments", confidence=0.9),
        _norm_result("NE555P", "NE555P", manufacturer="Texas Instruments", confidence=0.9),
    ]

    with patch("app.routers.ai.settings") as mock_settings, \
         patch("app.services.ai_part_normalizer.gradient_json", new_callable=AsyncMock) as mock:
        mock_settings.ai_features_enabled = "all"
        mock.return_value = mock_result
        resp = client.post(
            "/api/ai/normalize-parts",
            json={"parts": ["LM358DR", "NE555P"]},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert data["parts"][0]["normalized"] == "LM358DR"
    assert data["parts"][1]["normalized"] == "NE555P"


def test_normalize_parts_endpoint_empty(client):
    """Rejects empty parts list."""
    with patch("app.routers.ai.settings") as mock_settings:
        mock_settings.ai_features_enabled = "all"
        resp = client.post(
            "/api/ai/normalize-parts",
            json={"parts": []},
        )
    assert resp.status_code == 422  # Pydantic validation


def test_normalize_parts_endpoint_ai_disabled(client):
    """Returns 403 when AI features are disabled."""
    with patch("app.routers.ai.settings") as mock_settings:
        mock_settings.ai_features_enabled = "off"
        resp = client.post(
            "/api/ai/normalize-parts",
            json={"parts": ["LM358DR"]},
        )
    assert resp.status_code == 403

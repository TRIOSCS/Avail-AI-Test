"""Tests for the AI inference fallback (infer_part) error/confidence handling.

Tests patch app.services.ai_inference_fallback.claude_structured
(imported at the top of ai_inference_fallback.py via
``from app.utils.claude_client import claude_structured``).

Behavior under test (in infer_part):
  1. ClaudeError (and subclasses) propagate — Claude outages must surface so the
     worker's circuit breaker sees them, not be swallowed into not_found.
  2. Non-Claude exceptions are caught and degrade to status "not_found".
  3. Confidence >= _MIN_CONFIDENCE (0.95) with a description => "ai_inferred".
  4. Confidence below the threshold => "not_found".

Depends on:
  - app.services.ai_inference_fallback.infer_part / InferenceResult
  - app.utils.claude_errors.ClaudeError / ClaudeRateLimitError
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_inference_fallback import InferenceResult, infer_part
from app.utils.claude_errors import ClaudeError, ClaudeRateLimitError


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_claude_error_propagates(mock_cs):
    # Core regression guard: a Claude backend failure must surface, not be
    # swallowed into not_found, so the worker's circuit breaker can react.
    mock_cs.side_effect = ClaudeRateLimitError("429")
    with pytest.raises(ClaudeError):
        await infer_part("X")


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_non_claude_error_returns_not_found(mock_cs):
    mock_cs.side_effect = ValueError("bug")
    result = await infer_part("X")
    assert isinstance(result, InferenceResult)
    assert result.status == "not_found"


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_high_confidence_returns_ai_inferred(mock_cs):
    mock_cs.return_value = {
        "description": "Tantalum capacitor 10uF",
        "category": "Capacitor",
        "confidence": 0.97,
    }
    result = await infer_part("X")
    assert result.status == "ai_inferred"
    assert result.description == "Tantalum capacitor 10uF"
    assert result.confidence == 0.97


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_low_confidence_returns_not_found(mock_cs):
    mock_cs.return_value = {
        "description": "Tantalum capacitor 10uF",
        "category": "Capacitor",
        "confidence": 0.80,
    }
    result = await infer_part("X")
    assert result.status == "not_found"

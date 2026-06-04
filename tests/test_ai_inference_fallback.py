from unittest.mock import AsyncMock, patch

import pytest

from app.services.ai_inference_fallback import infer_part


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_confident_inference_returns_ai_inferred(mock_claude):
    mock_claude.return_value = {
        "description": "Linear voltage regulator, adjustable, TO-220",
        "category": "Voltage Regulator",
        "confidence": 0.8,
    }
    result = await infer_part("LM317T")
    assert result.status == "ai_inferred"
    assert result.description.startswith("Linear voltage regulator")
    assert result.category == "Voltage Regulator"
    # Opus must be requested
    assert mock_claude.call_args.kwargs["model_tier"] == "opus"


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_declined_inference_returns_not_found(mock_claude):
    mock_claude.return_value = {"description": "", "category": "", "confidence": 0.0}
    result = await infer_part("04M3HJ")
    assert result.status == "not_found"
    assert result.description is None


@pytest.mark.asyncio
@patch("app.services.ai_inference_fallback.claude_structured", new_callable=AsyncMock)
async def test_null_response_returns_not_found(mock_claude):
    mock_claude.return_value = None
    result = await infer_part("ZZZ999")
    assert result.status == "not_found"

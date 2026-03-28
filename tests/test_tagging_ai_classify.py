"""Tests for app/services/tagging_ai_classify.py — AI classification with mocked Claude.

Called by: pytest
Depends on: conftest fixtures, mocked claude_client
"""

from unittest.mock import AsyncMock, patch

from app.services.tagging_ai_classify import classify_parts_with_ai


class TestClassifyPartsWithAi:
    async def test_successful_classification(self):
        mock_result = [
            {
                "mpn": "STM32F103",
                "manufacturer": "STMicroelectronics",
                "category": "Microcontrollers (MCU)",
                "confidence": 0.95,
            },
            {
                "mpn": "LM317T",
                "manufacturer": "Texas Instruments",
                "category": "Voltage Regulators",
                "confidence": 0.92,
            },
        ]
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            result = await classify_parts_with_ai(["STM32F103", "LM317T"])
        assert len(result) == 2
        assert result[0]["manufacturer"] == "STMicroelectronics"
        assert result[1]["category"] == "Voltage Regulators"

    async def test_claude_unavailable_returns_unknown(self):
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch(
            "app.utils.claude_client.claude_json",
            new_callable=AsyncMock,
            side_effect=ClaudeUnavailableError("not configured"),
        ):
            result = await classify_parts_with_ai(["ABC123"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"
        assert result[0]["category"] == "Miscellaneous"

    async def test_claude_error_returns_fallback(self):
        from app.utils.claude_errors import ClaudeError

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, side_effect=ClaudeError("timeout")):
            result = await classify_parts_with_ai(["XYZ789"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"

    async def test_invalid_response_returns_fallback(self):
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value="not a list"):
            result = await classify_parts_with_ai(["BAD123"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"

    async def test_empty_response_returns_fallback(self):
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=None):
            result = await classify_parts_with_ai(["EMPTY1"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"

    async def test_null_manufacturer_normalized(self):
        mock_result = [
            {"mpn": "CUSTOM123", "manufacturer": None, "category": None, "confidence": 0.3},
        ]
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            result = await classify_parts_with_ai(["CUSTOM123"])
        assert len(result) == 1
        assert result[0]["manufacturer"] == "Unknown"
        assert result[0]["category"] == "Miscellaneous"

    async def test_multiple_parts_batch(self):
        mpns = ["STM32F103", "LM317T", "IRF540N", "GRM188R61E106MA73"]
        mock_result = [
            {"mpn": m, "manufacturer": f"Mfr-{i}", "category": f"Cat-{i}", "confidence": 0.95}
            for i, m in enumerate(mpns)
        ]
        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_result):
            result = await classify_parts_with_ai(mpns)
        assert len(result) == 4
        assert all(r["mpn"] for r in result)

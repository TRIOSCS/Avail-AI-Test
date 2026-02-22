"""
test_trouble_prompt.py — Unit tests for the AI trouble prompt generator.

Tests the generate_trouble_prompt() function with mocked gradient_json calls.

Called by: pytest
Depends on: app/services/ai_trouble_prompt.py
"""

import asyncio
from unittest.mock import AsyncMock, patch


def _run(coro):
    """Run an async coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


MOCK_RESULT = {
    "title": "Submit button unresponsive on RFQ view",
    "prompt": "Investigate the submit button in the RFQ view...",
}


class TestGenerateTroublePrompt:
    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_basic_generation(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.return_value = MOCK_RESULT
        result = _run(generate_trouble_prompt(
            user_message="The submit button doesn't work",
            current_view="rfq",
            reporter_name="Test User",
        ))
        assert result is not None
        assert result["title"] == MOCK_RESULT["title"]
        assert result["prompt"] == MOCK_RESULT["prompt"]
        mock_gradient.assert_called_once()

    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_console_errors_included(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.return_value = MOCK_RESULT
        _run(generate_trouble_prompt(
            user_message="Page crashed",
            console_errors='[{"msg":"TypeError: undefined is not a function","ts":123}]',
            reporter_name="Tester",
        ))
        # The prompt sent to gradient should contain the console errors
        call_args = mock_gradient.call_args
        prompt_text = call_args[0][0]  # first positional arg
        assert "TypeError" in prompt_text

    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_view_context_maps_to_files(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.return_value = MOCK_RESULT
        _run(generate_trouble_prompt(
            user_message="CRM view is broken",
            current_view="crm",
            reporter_name="Tester",
        ))
        call_args = mock_gradient.call_args
        prompt_text = call_args[0][0]
        assert "crm.js" in prompt_text

    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_gradient_failure_returns_none(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.return_value = None
        result = _run(generate_trouble_prompt(
            user_message="Something is wrong",
            reporter_name="Tester",
        ))
        assert result is None

    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_gradient_exception_returns_none(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.side_effect = Exception("API timeout")
        result = _run(generate_trouble_prompt(
            user_message="Something is wrong",
            reporter_name="Tester",
        ))
        assert result is None

    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_missing_title_in_response_returns_none(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.return_value = {"prompt": "some prompt but no title"}
        result = _run(generate_trouble_prompt(
            user_message="Issue here",
            reporter_name="Tester",
        ))
        assert result is None

    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_missing_prompt_in_response_returns_none(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.return_value = {"title": "Some Title"}
        result = _run(generate_trouble_prompt(
            user_message="Issue here",
            reporter_name="Tester",
        ))
        assert result is None

    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_empty_fields_handled(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.return_value = MOCK_RESULT
        result = _run(generate_trouble_prompt(
            user_message="Minimal report",
        ))
        assert result is not None
        assert result["title"] == MOCK_RESULT["title"]

    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_screenshot_flag_included(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.return_value = MOCK_RESULT
        _run(generate_trouble_prompt(
            user_message="Visual bug",
            has_screenshot=True,
            reporter_name="Tester",
        ))
        call_args = mock_gradient.call_args
        prompt_text = call_args[0][0]
        assert "screenshot" in prompt_text.lower()

    @patch("app.services.ai_trouble_prompt.gradient_json", new_callable=AsyncMock)
    def test_nested_view_path(self, mock_gradient):
        from app.services.ai_trouble_prompt import generate_trouble_prompt

        mock_gradient.return_value = MOCK_RESULT
        _run(generate_trouble_prompt(
            user_message="Issue in vendors/sourcing",
            current_view="Vendors/sourcing",
            reporter_name="Tester",
        ))
        call_args = mock_gradient.call_args
        prompt_text = call_args[0][0]
        assert "sourcing" in prompt_text.lower()

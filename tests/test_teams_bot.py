"""Tests for Teams bot service (Phase 3).

Tests intent classification, query handlers, card builders,
conversation context, and webhook validation.
"""

import base64
import hashlib
import hmac as hmac_module
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "1")


# ── Intent Classification ────────────────────────────────────────────


class TestIntentClassification:
    @pytest.mark.asyncio
    async def test_help_intent(self):
        from app.services.teams_bot_service import classify_intent
        intent, params = await classify_intent("help", "user1")
        assert intent == "help"

    @pytest.mark.asyncio
    async def test_question_mark_is_help(self):
        from app.services.teams_bot_service import classify_intent
        intent, _ = await classify_intent("?", "user1")
        assert intent == "help"

    @pytest.mark.asyncio
    async def test_pipeline_intent(self):
        from app.services.teams_bot_service import classify_intent
        intent, _ = await classify_intent("what's my pipeline?", "user1")
        assert intent == "pipeline_status"

    @pytest.mark.asyncio
    async def test_my_deals_intent(self):
        from app.services.teams_bot_service import classify_intent
        intent, _ = await classify_intent("show my deals", "user1")
        assert intent == "pipeline_status"

    @pytest.mark.asyncio
    async def test_recent_quotes_intent(self):
        from app.services.teams_bot_service import classify_intent
        intent, _ = await classify_intent("my recent quotes", "user1")
        assert intent == "recent_quotes"

    @pytest.mark.asyncio
    async def test_deal_info_with_number(self):
        from app.services.teams_bot_service import classify_intent
        intent, params = await classify_intent("deal #123", "user1")
        assert intent == "deal_info"
        assert params["requisition_id"] == 123

    @pytest.mark.asyncio
    async def test_req_info_with_number(self):
        from app.services.teams_bot_service import classify_intent
        intent, params = await classify_intent("req 456", "user1")
        assert intent == "deal_info"
        assert params["requisition_id"] == 456

    @pytest.mark.asyncio
    async def test_vendor_lookup(self):
        from app.services.teams_bot_service import classify_intent
        intent, params = await classify_intent("vendor Acme Corp", "user1")
        assert intent == "vendor_lookup"
        assert "Acme Corp" in params.get("name", "")

    @pytest.mark.asyncio
    async def test_company_info(self):
        from app.services.teams_bot_service import classify_intent
        intent, params = await classify_intent("company Tesla", "user1")
        assert intent == "company_info"
        assert "Tesla" in params.get("name", "")

    @pytest.mark.asyncio
    async def test_risk_intent_no_id(self):
        from app.services.teams_bot_service import classify_intent
        intent, params = await classify_intent("what's at risk?", "user1")
        assert intent == "deal_risk"

    @pytest.mark.asyncio
    async def test_risk_intent_with_id(self):
        from app.services.teams_bot_service import classify_intent
        intent, params = await classify_intent("risk for 789", "user1")
        assert intent == "deal_risk"
        assert params.get("requisition_id") == 789


# ── Card Builders ────────────────────────────────────────────────────


class TestCardBuilders:
    def test_text_card(self):
        from app.services.teams_bot_service import _text_card
        card = _text_card("Hello world")
        assert card["type"] == "message"
        assert len(card["attachments"]) == 1
        content = card["attachments"][0]["content"]
        assert content["type"] == "AdaptiveCard"
        assert content["body"][0]["text"] == "Hello world"

    def test_facts_card(self):
        from app.services.teams_bot_service import _facts_card
        card = _facts_card("Title", "Subtitle", [{"title": "Key", "value": "Val"}])
        content = card["attachments"][0]["content"]
        assert content["body"][0]["text"] == "Title"
        assert any(b.get("type") == "FactSet" for b in content["body"])


# ── Helpers ──────────────────────────────────────────────────────────


class TestHelpers:
    def test_extract_number(self):
        from app.services.teams_bot_service import _extract_number
        assert _extract_number("deal #123") == 123
        assert _extract_number("no number here") is None

    def test_extract_name(self):
        from app.services.teams_bot_service import _extract_name
        assert _extract_name("vendor Acme Corp", ["vendor"]) == "Acme Corp"
        assert _extract_name("company about Tesla", ["company"]) == "Tesla"


# ── Handler Tests ────────────────────────────────────────────────────


class TestHandlers:
    @pytest.mark.asyncio
    async def test_help_handler(self):
        from app.services.teams_bot_service import _handle_help
        result = await _handle_help("John", "aad1", {})
        text = result["attachments"][0]["content"]["body"][0]["text"]
        assert "pipeline" in text.lower()
        assert "help" in text.lower()

    @pytest.mark.asyncio
    async def test_unknown_handler(self):
        from app.services.teams_bot_service import _handle_unknown
        result = await _handle_unknown("John", "aad1", {})
        text = result["attachments"][0]["content"]["body"][0]["text"]
        assert "not sure" in text.lower()

    @pytest.mark.asyncio
    async def test_deal_info_no_id(self):
        from app.services.teams_bot_service import _handle_deal_info
        result = await _handle_deal_info("John", "aad1", {})
        text = result["attachments"][0]["content"]["body"][0]["text"]
        assert "which" in text.lower()


# ── HMAC Validation ──────────────────────────────────────────────────


class TestHmacValidation:
    def test_valid_hmac(self):
        from app.routers.teams_bot import _validate_hmac
        secret = base64.b64encode(b"test-secret").decode()
        body = b'{"text": "hello"}'
        sig = base64.b64encode(
            hmac_module.new(b"test-secret", body, hashlib.sha256).digest()
        ).decode()
        assert _validate_hmac(body, f"HMAC {sig}", secret) is True

    def test_invalid_hmac(self):
        from app.routers.teams_bot import _validate_hmac
        secret = base64.b64encode(b"test-secret").decode()
        assert _validate_hmac(b"body", "HMAC invalid", secret) is False

    def test_malformed_secret(self):
        from app.routers.teams_bot import _validate_hmac
        assert _validate_hmac(b"body", "HMAC sig", "not-base64!!!") is False


# ── Bot Router ───────────────────────────────────────────────────────


class TestBotRouter:
    def test_card_response(self):
        from app.routers.teams_bot import _card_response
        card = _card_response("Test message")
        assert card["type"] == "message"
        assert card["attachments"][0]["content"]["body"][0]["text"] == "Test message"


# ── Conversation Context ────────────────────────────────────────────


class TestConversationContext:
    def test_context_without_redis_is_noop(self):
        from app.services.teams_bot_service import _update_context, _get_context
        _update_context("user1", "test message")
        ctx = _get_context("user1")
        assert ctx == []  # No Redis in testing

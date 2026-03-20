"""
tests/test_proactive_email.py -- Tests for services/proactive_email.py

Covers: AI email drafting, fallback template, HTML builder, parts formatter.

Called by: pytest
Depends on: app/services/proactive_email.py
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.proactive_email import (
    _build_html,
    _fallback_draft,
    _format_parts,
    draft_proactive_email,
)

# ── _format_parts ───────────────────────────────────────────────────


class TestFormatParts:
    def test_basic_parts(self):
        parts = [
            {"mpn": "LM317T", "manufacturer": "TI", "qty": 100, "sell_price": 1.5},
            {"mpn": "LM7805", "qty": 50},
        ]
        result = _format_parts(parts)
        assert "1. LM317T (TI)" in result
        assert "Qty: 100" in result
        assert "$1.5000" in result
        assert "2. LM7805" in result
        assert "Qty: 50" in result

    def test_customer_history_fields(self):
        parts = [
            {
                "mpn": "SN74HC00",
                "qty": 200,
                "customer_purchase_count": 5,
                "customer_last_purchased_at": "Jan 2025",
            }
        ]
        result = _format_parts(parts)
        assert "bought 5x before" in result
        assert "Last purchased: Jan 2025" in result

    def test_truncate_at_15(self):
        parts = [{"mpn": f"PART{i}", "qty": 1} for i in range(20)]
        result = _format_parts(parts)
        assert "15." in result
        assert "16." not in result

    def test_empty_parts(self):
        assert _format_parts([]) == ""

    def test_missing_fields(self):
        parts = [{"mpn": "X123"}]
        result = _format_parts(parts)
        assert "X123" in result
        assert "Qty: ?" in result


# ── _build_html ─────────────────────────────────────────────────────


class TestBuildHtml:
    def test_basic_html(self):
        parts = [
            {
                "mpn": "LM317T",
                "manufacturer": "TI",
                "qty": 100,
                "sell_price": 1.5,
                "condition": "New",
                "lead_time": "2 weeks",
            },
        ]
        html = _build_html("Great parts!", "Alice", parts, "Bob", None)
        assert "Hi Alice," in html
        assert "Great parts!" in html
        assert "LM317T" in html
        assert "$1.5000" in html
        assert "Bob" in html
        assert "Trio Supply Chain Solutions" in html

    def test_no_contact_name(self):
        html = _build_html("Body text", None, [], "Sales Rep", None)
        assert "Hello," in html
        assert "Hi " not in html

    def test_notes_included(self):
        html = _build_html("Body", "Tom", [], "Rep", "Special discount available")
        assert "Special discount available" in html

    def test_notes_none(self):
        html = _build_html("Body", "Tom", [], "Rep", None)
        assert "Special discount" not in html

    def test_html_escaping(self):
        html = _build_html("Body", "<script>alert(1)</script>", [], "Rep", None)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_no_salesperson_name(self):
        html = _build_html("Body", None, [], "", None)
        assert "Trio Supply Chain Solutions" in html


# ── _fallback_draft ─────────────────────────────────────────────────


class TestFallbackDraft:
    def test_returns_dict(self):
        parts = [
            {
                "mpn": "LM317T",
                "manufacturer": "TI",
                "qty": 100,
                "sell_price": 1.5,
                "condition": "New",
                "lead_time": "2 weeks",
            }
        ]
        result = _fallback_draft("Acme Corp", "Alice", parts, "Bob", None)
        assert "subject" in result
        assert "body" in result
        assert "html" in result
        assert "Acme Corp" in result["subject"]

    def test_fallback_with_many_parts(self):
        parts = [
            {"mpn": f"PART{i}", "qty": 1, "sell_price": 0.5, "manufacturer": "", "condition": "", "lead_time": ""}
            for i in range(5)
        ]
        result = _fallback_draft("BigCo", None, parts, "Rep", "hurry up")
        # All 5 parts appear in the HTML table
        assert "PART0" in result["html"]
        assert "PART4" in result["html"]

    def test_fallback_body_content(self):
        parts = [{"mpn": "ABC", "qty": 1, "sell_price": 1.0, "manufacturer": "", "condition": "", "lead_time": ""}]
        result = _fallback_draft("TestCo", None, parts, "Rep", None)
        assert "inventory" in result["body"].lower() or "sourced" in result["body"].lower()


# ── draft_proactive_email ───────────────────────────────────────────


class TestDraftProactiveEmail:
    @pytest.mark.asyncio
    @patch("app.services.proactive_email.claude_json", new_callable=AsyncMock)
    async def test_successful_draft(self, mock_claude):
        mock_claude.return_value = {
            "subject": "Parts for You",
            "body": "We have great parts available for your needs.",
        }
        result = await draft_proactive_email(
            company_name="Acme Corp",
            contact_name="Alice",
            parts=[
                {
                    "mpn": "LM317T",
                    "manufacturer": "TI",
                    "qty": 100,
                    "sell_price": 1.5,
                    "condition": "New",
                    "lead_time": "2 weeks",
                }
            ],
            salesperson_name="Bob",
        )
        assert result is not None
        assert result["subject"] == "Parts for You"
        assert result["body"] == "We have great parts available for your needs."
        assert "html" in result
        assert "Alice" in result["html"]

    @pytest.mark.asyncio
    @patch("app.services.proactive_email.claude_json", new_callable=AsyncMock)
    async def test_empty_parts_returns_none(self, mock_claude):
        result = await draft_proactive_email(
            company_name="Acme",
            contact_name=None,
            parts=[],
        )
        assert result is None
        mock_claude.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.proactive_email.claude_json", new_callable=AsyncMock)
    async def test_ai_returns_none_fallback(self, mock_claude):
        mock_claude.return_value = None
        result = await draft_proactive_email(
            company_name="Acme",
            contact_name=None,
            parts=[{"mpn": "X1", "qty": 10, "sell_price": 1.0, "manufacturer": "", "condition": "", "lead_time": ""}],
        )
        assert result is not None
        assert "subject" in result
        assert "Acme" in result["subject"]

    @pytest.mark.asyncio
    @patch("app.services.proactive_email.claude_json", new_callable=AsyncMock)
    async def test_ai_returns_empty_body_fallback(self, mock_claude):
        mock_claude.return_value = {"subject": "Hi", "body": ""}
        result = await draft_proactive_email(
            company_name="BigCo",
            contact_name="Tom",
            parts=[{"mpn": "Y2", "qty": 5, "sell_price": 2.0, "manufacturer": "", "condition": "", "lead_time": ""}],
        )
        assert result is not None
        assert result["body"]  # non-empty fallback body

    @pytest.mark.asyncio
    @patch("app.services.proactive_email.claude_json", new_callable=AsyncMock)
    async def test_ai_returns_no_subject(self, mock_claude):
        mock_claude.return_value = {"subject": "", "body": "Here are some parts."}
        result = await draft_proactive_email(
            company_name="TestCo",
            contact_name=None,
            parts=[{"mpn": "Z3", "qty": 1, "sell_price": 0.5, "manufacturer": "", "condition": "", "lead_time": ""}],
        )
        assert result["subject"] == "Parts Available — TestCo"

    @pytest.mark.asyncio
    @patch("app.services.proactive_email.claude_json", new_callable=AsyncMock)
    async def test_with_notes(self, mock_claude):
        mock_claude.return_value = {
            "subject": "Special Offer",
            "body": "Discount available on these components.",
        }
        result = await draft_proactive_email(
            company_name="Acme",
            contact_name="Alice",
            parts=[
                {
                    "mpn": "A1",
                    "qty": 50,
                    "sell_price": 3.0,
                    "manufacturer": "TI",
                    "condition": "New",
                    "lead_time": "1 week",
                }
            ],
            notes="10% discount for repeat customers",
        )
        assert result is not None
        # Verify notes were included in the prompt sent to AI
        call_args = mock_claude.call_args
        assert "10% discount" in call_args[0][0]

    @pytest.mark.asyncio
    @patch("app.services.proactive_email.claude_json", new_callable=AsyncMock)
    async def test_ai_returns_non_dict_fallback(self, mock_claude):
        mock_claude.return_value = "not a dict"
        result = await draft_proactive_email(
            company_name="Acme",
            contact_name=None,
            parts=[{"mpn": "B2", "qty": 1, "sell_price": 1.0, "manufacturer": "", "condition": "", "lead_time": ""}],
        )
        assert result is not None
        assert "Acme" in result["subject"]

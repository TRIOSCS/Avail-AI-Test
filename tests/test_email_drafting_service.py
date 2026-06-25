"""Tests for the unified AI email drafting service (app/services/email_drafting.py).

The service exposes a single dispatcher ``draft_email(kind, context)`` that powers
three surfaces: RFQ rephrase, vendor reply, and follow-up. Each path degrades
gracefully when Claude is unavailable.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.services import email_drafting
from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError


# ── RFQ rephrase ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_rfq_rephrase_returns_rephrased_body():
    with patch.object(email_drafting, "rephrase_rfq", new_callable=AsyncMock) as m:
        m.return_value = "Hello team, please quote the below."
        result = await email_drafting.draft_email("rfq_rephrase", {"body": "ORIGINAL BODY"})
    assert result == {"body": "Hello team, please quote the below."}
    m.assert_awaited_once()


@pytest.mark.asyncio
async def test_rfq_rephrase_falls_back_to_original_when_ai_returns_none():
    with patch.object(email_drafting, "rephrase_rfq", new_callable=AsyncMock) as m:
        m.return_value = None
        result = await email_drafting.draft_email("rfq_rephrase", {"body": "ORIGINAL BODY"})
    assert result == {"body": "ORIGINAL BODY"}


@pytest.mark.asyncio
async def test_rfq_rephrase_falls_back_to_original_on_claude_error():
    with patch.object(email_drafting, "rephrase_rfq", new_callable=AsyncMock) as m:
        m.side_effect = ClaudeError("boom")
        result = await email_drafting.draft_email("rfq_rephrase", {"body": "ORIGINAL BODY"})
    assert result == {"body": "ORIGINAL BODY"}


# ── Follow-up ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_follow_up_returns_ai_body():
    with patch.object(email_drafting, "claude_text", new_callable=AsyncMock) as m:
        m.return_value = "Hi Acme, following up on LM358N (500 pcs) after 7 days."
        result = await email_drafting.draft_email(
            "follow_up",
            {
                "vendor_name": "Acme",
                "parts": "LM358N",
                "days_waiting": 7,
                "subject": "RFQ - LM358N",
            },
        )
    assert result is not None
    assert "following up" in result["body"].lower()


@pytest.mark.asyncio
async def test_follow_up_falls_back_to_template_on_failure():
    with patch.object(email_drafting, "claude_text", new_callable=AsyncMock) as m:
        m.side_effect = ClaudeUnavailableError("no key")
        result = await email_drafting.draft_email(
            "follow_up",
            {"vendor_name": "Acme", "parts": "LM358N", "days_waiting": 7},
        )
    assert result is not None
    # Fallback still produces a usable, vendor-addressed body.
    assert "Acme" in result["body"]
    assert result["body"].strip() != ""


# ── Vendor reply ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_vendor_reply_returns_subject_and_body():
    with patch.object(email_drafting, "claude_json", new_callable=AsyncMock) as m:
        m.return_value = {
            "subject": "Re: RFQ - LM358N",
            "body": "Thanks for the quote. We can accept 5,000 pcs at $0.38.",
        }
        result = await email_drafting.draft_email(
            "vendor_reply",
            {
                "classification": "quote_provided",
                "vendor_name": "Acme",
                "mpn": "LM358N",
                "qty": 5000,
                "price": 0.38,
                "subject": "RFQ - LM358N",
            },
        )
    assert result == {
        "subject": "Re: RFQ - LM358N",
        "body": "Thanks for the quote. We can accept 5,000 pcs at $0.38.",
    }


@pytest.mark.asyncio
async def test_vendor_reply_returns_none_on_failure():
    with patch.object(email_drafting, "claude_json", new_callable=AsyncMock) as m:
        m.side_effect = ClaudeError("boom")
        result = await email_drafting.draft_email(
            "vendor_reply",
            {"classification": "quote_provided", "vendor_name": "Acme"},
        )
    assert result is None


# ── Guardrail: unknown kind ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unknown_kind_raises_value_error():
    with pytest.raises(ValueError):
        await email_drafting.draft_email("nonsense", {})

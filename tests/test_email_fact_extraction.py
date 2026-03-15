"""Tests for email fact extraction in email_intelligence_service.

Tests the extract_durable_facts() function which uses AI to extract
durable facts (lead times, MOQs, EOL notices, etc.) from vendor emails
and stores them in the knowledge ledger.

Called by: pytest
Depends on: app/services/email_intelligence_service.py
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.email_intelligence_service import (
    ALLOWED_CLASSIFICATIONS,
    FACT_EXPIRY_DEFAULTS,
    extract_durable_facts,
)


@pytest.fixture
def mock_db():
    """Create a mock database session with chainable query mock."""
    db = MagicMock()
    # The function calls db.query(VendorCard).filter(...).first() for vendor lookup
    # and db.query(KnowledgeEntry).filter(...).filter(...).count() for dedup.
    # Since both use db.query, we need a flexible mock.
    # Default: no vendor found, no dedup matches.
    filter_chain = MagicMock()
    filter_chain.filter.return_value = filter_chain
    filter_chain.count.return_value = 0
    filter_chain.first.return_value = None
    db.query.return_value.filter.return_value = filter_chain
    return db


def _make_db_mock(dedup_count=0, vendor=None):
    """Create a mock db session with specific dedup/vendor behavior."""
    db = MagicMock()
    filter_chain = MagicMock()
    filter_chain.filter.return_value = filter_chain
    filter_chain.count.return_value = dedup_count
    filter_chain.first.return_value = vendor
    db.query.return_value.filter.return_value = filter_chain
    return db


@pytest.mark.asyncio
@patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
@patch("app.services.knowledge_service.create_entry")
async def test_extracts_lead_time_fact(mock_create_entry, mock_claude):
    """AI returns a lead_time fact -> stored via create_entry."""
    mock_claude.return_value = {
        "facts": [
            {
                "fact_type": "lead_time",
                "value": "12-14 weeks ARO",
                "mpn": "LM317T",
                "confidence": 0.9,
            }
        ]
    }
    fake_entry = MagicMock()
    fake_entry.id = 1
    mock_create_entry.return_value = fake_entry

    db = _make_db_mock(dedup_count=0)

    result = await extract_durable_facts(
        db,
        body="We can offer LM317T with 12-14 weeks lead time ARO. MOQ 1000 pcs." * 2,
        sender_email="sales@acmedist.com",
        sender_name="John Doe",
        classification="offer",
        parsed_quotes={"parts": [{"mpn": "LM317T", "price": 0.50}]},
        user_id=1,
    )

    assert len(result) == 1
    mock_claude.assert_called_once()
    mock_create_entry.assert_called_once()
    call_kwargs = mock_create_entry.call_args[1]
    assert call_kwargs["entry_type"] == "fact"
    assert call_kwargs["source"] == "email_parsed"
    assert "[lead_time]" in call_kwargs["content"]
    assert call_kwargs["mpn"] == "LM317T"


@pytest.mark.asyncio
@patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
async def test_skips_non_offer_emails(mock_claude):
    """Classification='general' -> no AI call, empty list."""
    db = _make_db_mock()

    result = await extract_durable_facts(
        db,
        body="Hello, just following up on our meeting last week." * 3,
        sender_email="john@example.com",
        sender_name="John",
        classification="general",
        parsed_quotes=None,
        user_id=1,
    )

    assert result == []
    mock_claude.assert_not_called()


@pytest.mark.asyncio
@patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
async def test_skips_short_body(mock_claude):
    """Body < 50 chars -> no AI call, empty list."""
    db = _make_db_mock()

    result = await extract_durable_facts(
        db,
        body="Short body",
        sender_email="sales@vendor.com",
        sender_name="Sales",
        classification="offer",
        parsed_quotes=None,
        user_id=1,
    )

    assert result == []
    mock_claude.assert_not_called()


@pytest.mark.asyncio
@patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
@patch("app.services.knowledge_service.create_entry")
async def test_dedup_skips_recent_duplicate(mock_create_entry, mock_claude):
    """Existing entry found within 7 days -> skip that fact."""
    mock_claude.return_value = {
        "facts": [
            {
                "fact_type": "lead_time",
                "value": "12 weeks",
                "mpn": "LM317T",
                "confidence": 0.9,
            }
        ]
    }

    # Simulate dedup query returning count > 0
    db = _make_db_mock(dedup_count=1)

    result = await extract_durable_facts(
        db,
        body="We can offer LM317T with 12 weeks lead time. Please confirm." * 2,
        sender_email="sales@acmedist.com",
        sender_name="John",
        classification="offer",
        parsed_quotes=None,
        user_id=1,
    )

    assert result == []
    mock_create_entry.assert_not_called()


@pytest.mark.asyncio
@patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
async def test_ai_returns_none_gracefully(mock_claude):
    """AI returns None -> empty list, no crash."""
    mock_claude.return_value = None
    db = _make_db_mock()

    result = await extract_durable_facts(
        db,
        body="This is a long enough email body to pass the 50 char threshold for processing.",
        sender_email="sales@vendor.com",
        sender_name="Sales",
        classification="offer",
        parsed_quotes=None,
        user_id=1,
    )

    assert result == []


@pytest.mark.asyncio
@patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
@patch("app.services.knowledge_service.create_entry")
async def test_multiple_facts_extracted(mock_create_entry, mock_claude):
    """AI returns 3 facts -> all 3 stored."""
    mock_claude.return_value = {
        "facts": [
            {"fact_type": "lead_time", "value": "8 weeks", "mpn": "LM317T", "confidence": 0.9},
            {"fact_type": "moq", "value": "500 pcs minimum", "mpn": "LM317T", "confidence": 0.85},
            {"fact_type": "availability", "value": "In stock, ready to ship", "mpn": "NE555P", "confidence": 0.95},
        ]
    }
    fake_entry = MagicMock()
    fake_entry.id = 1
    mock_create_entry.return_value = fake_entry

    db = _make_db_mock(dedup_count=0)

    result = await extract_durable_facts(
        db,
        body="LM317T: 8 weeks lead time, MOQ 500 pcs. NE555P in stock ready to ship." * 2,
        sender_email="sales@vendor.com",
        sender_name="Sales",
        classification="quote_reply",
        parsed_quotes=None,
        user_id=1,
    )

    assert len(result) == 3
    assert mock_create_entry.call_count == 3


@pytest.mark.asyncio
@patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
@patch("app.services.knowledge_service.create_entry")
async def test_skips_invalid_fact_type(mock_create_entry, mock_claude):
    """AI returns unknown fact_type -> skipped, only valid ones stored."""
    mock_claude.return_value = {
        "facts": [
            {"fact_type": "lead_time", "value": "12-14 weeks ARO", "confidence": 0.9},
            {"fact_type": "bogus_type", "value": "should be skipped", "confidence": 0.9},
        ]
    }
    fake_entry = MagicMock(id=1)
    mock_create_entry.return_value = fake_entry

    db = _make_db_mock(dedup_count=0)

    result = await extract_durable_facts(
        db,
        body="A" * 100,
        sender_email="vendor@example.com",
        sender_name="Vendor Rep",
        classification="stock_list",
        parsed_quotes=None,
        user_id=1,
    )

    assert len(result) == 1
    mock_create_entry.assert_called_once()


@pytest.mark.asyncio
@patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
async def test_exception_returns_empty_list(mock_claude):
    """Any exception in extract_durable_facts -> returns [], no crash."""
    mock_claude.side_effect = RuntimeError("API down")
    db = _make_db_mock()

    result = await extract_durable_facts(
        db,
        body="A" * 100,
        sender_email="vendor@example.com",
        sender_name="Vendor Rep",
        classification="offer",
        parsed_quotes=None,
        user_id=1,
    )

    assert result == []


def test_constants_defined():
    """Verify ALLOWED_CLASSIFICATIONS and FACT_EXPIRY_DEFAULTS are correct."""
    assert ALLOWED_CLASSIFICATIONS == {"offer", "quote_reply", "stock_list"}
    assert FACT_EXPIRY_DEFAULTS["lead_time"] == 180
    assert FACT_EXPIRY_DEFAULTS["moq"] == 90
    assert FACT_EXPIRY_DEFAULTS["eol_notice"] is None
    assert FACT_EXPIRY_DEFAULTS["availability"] == 30
    assert "vendor_policy" in FACT_EXPIRY_DEFAULTS

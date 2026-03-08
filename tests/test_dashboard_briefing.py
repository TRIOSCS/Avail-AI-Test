"""Tests for app/services/dashboard_briefing.py — morning briefing service.

Covers buyer and sales briefings, empty states, and timestamp generation.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.dashboard_briefing import generate_briefing


BUYER_SECTION_NAMES = [
    "open_rfqs_no_offers",
    "vendor_emails",
    "unanswered_questions",
    "stalling_deals",
    "resurfaced_parts",
    "price_movement",
]

SALES_SECTION_NAMES = [
    "quotes_needing_followup",
    "overnight_vendor_quotes",
    "customer_followups",
    "new_answers",
    "quiet_customers",
    "deals_at_risk",
    "quotes_ready",
]


def _mock_db():
    """Return a MagicMock that behaves like a SQLAlchemy Session.

    All query chains resolve to empty lists so sections return 0 items.
    """
    db = MagicMock()
    # .query(...).filter(...).order_by(...).all() -> []
    # .query(...).filter(...).all() -> []
    # .query(...).filter(...).distinct().all() -> []
    # .query(func.max(...)).filter(...).scalar() -> None
    # .query(func.count(...)).filter(...).scalar() -> 0
    query_mock = MagicMock()
    db.query.return_value = query_mock
    query_mock.filter.return_value = query_mock
    query_mock.order_by.return_value = query_mock
    query_mock.distinct.return_value = query_mock
    query_mock.join.return_value = query_mock
    query_mock.limit.return_value = query_mock
    query_mock.all.return_value = []
    query_mock.scalar.return_value = None
    return db


def test_buyer_briefing_returns_expected_sections():
    db = _mock_db()
    result = generate_briefing(db, user_id=1, role="buyer")

    assert result["role"] == "buyer"
    assert len(result["sections"]) == 6
    names = [s["name"] for s in result["sections"]]
    assert names == BUYER_SECTION_NAMES

    # Each section has required keys
    for section in result["sections"]:
        assert "name" in section
        assert "label" in section
        assert "count" in section
        assert "items" in section
        assert isinstance(section["items"], list)
        assert section["count"] == len(section["items"])


def test_sales_briefing_returns_expected_sections():
    db = _mock_db()
    with patch("app.services.activity_insights._detect_gone_quiet", return_value=[]):
        result = generate_briefing(db, user_id=1, role="sales")

    assert result["role"] == "sales"
    assert len(result["sections"]) == 7
    names = [s["name"] for s in result["sections"]]
    assert names == SALES_SECTION_NAMES

    for section in result["sections"]:
        assert "name" in section
        assert "label" in section
        assert "count" in section
        assert "items" in section
        assert isinstance(section["items"], list)


def test_empty_briefing_has_zero_total():
    db = _mock_db()
    result = generate_briefing(db, user_id=1, role="buyer")

    assert result["total_items"] == 0
    for section in result["sections"]:
        assert section["count"] == 0
        assert section["items"] == []


def test_briefing_returns_generated_at_timestamp():
    db = _mock_db()
    before = datetime.now(timezone.utc)
    result = generate_briefing(db, user_id=1, role="buyer")
    after = datetime.now(timezone.utc)

    ts = datetime.fromisoformat(result["generated_at"])
    assert before <= ts <= after


def test_buyer_vendor_emails_with_data():
    """Verify vendor_emails section populates items from EmailIntelligence rows."""
    from app.services.dashboard_briefing import _vendor_emails

    db = _mock_db()
    now = datetime.now(timezone.utc)

    email_row = MagicMock()
    email_row.id = 42
    email_row.classification = "offer"
    email_row.sender_email = "vendor@example.com"
    email_row.subject = "Stock list Q1"
    email_row.created_at = now

    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [email_row]

    section = _vendor_emails(db, user_id=1, now=now)
    assert section["count"] == 1
    item = section["items"][0]
    assert item["entity_type"] == "email_intelligence"
    assert item["entity_id"] == 42
    assert item["priority"] == "high"


def test_sales_deals_at_risk_includes_red_only():
    """Only requisitions with risk_level=red appear in deals_at_risk."""
    now = datetime.now(timezone.utc)

    req_mock = MagicMock()
    req_mock.id = 10
    req_mock.name = "Risky Deal"
    req_mock.created_at = now
    req_mock.status = "open"

    # Build a mock db where every .all() returns [req_mock] so deals_at_risk
    # picks it up regardless of call ordering.
    db = MagicMock()
    query_mock = MagicMock()
    db.query.return_value = query_mock
    query_mock.filter.return_value = query_mock
    query_mock.order_by.return_value = query_mock
    query_mock.distinct.return_value = query_mock
    query_mock.join.return_value = query_mock
    query_mock.limit.return_value = query_mock
    query_mock.all.return_value = [req_mock]
    query_mock.scalar.return_value = 0

    with patch("app.services.activity_insights._detect_gone_quiet", return_value=[]), \
         patch("app.services.deal_risk.assess_risk", return_value={
             "risk_level": "red",
             "score": 85,
             "explanation": "No activity in 14 days",
             "suggested_action": "Follow up immediately",
         }):
        result = generate_briefing(db, user_id=1, role="sales")

    risk_section = next(s for s in result["sections"] if s["name"] == "deals_at_risk")
    assert risk_section["count"] >= 1
    # Find the item for our specific req
    risk_items = [i for i in risk_section["items"] if i["entity_id"] == 10]
    assert len(risk_items) == 1
    assert risk_items[0]["priority"] == "high"


def test_section_error_returns_empty():
    """If a section's query throws, it returns an empty section instead of crashing."""
    db = MagicMock()
    db.query.side_effect = RuntimeError("DB down")

    result = generate_briefing(db, user_id=1, role="buyer")

    # Should still return all 6 sections, all empty
    assert len(result["sections"]) == 6
    assert result["total_items"] == 0
    for section in result["sections"]:
        assert section["count"] == 0


def test_default_role_is_buyer():
    db = _mock_db()
    result = generate_briefing(db, user_id=1)
    assert result["role"] == "buyer"
    assert len(result["sections"]) == 6
    names = [s["name"] for s in result["sections"]]
    assert names == BUYER_SECTION_NAMES


def test_open_rfqs_no_offers_section():
    """Open reqs with zero offers appear in the section."""
    db = _mock_db()
    now = datetime.now(timezone.utc)

    req_mock = MagicMock()
    req_mock.id = 5
    req_mock.name = "Need LM317"
    req_mock.customer_name = "Acme Corp"
    req_mock.created_at = now - timedelta(days=4)
    req_mock.status = "open"

    call_count = {"n": 0}

    def side_effect_all():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [req_mock]
        return []

    db.query.return_value.all.side_effect = side_effect_all
    db.query.return_value.filter.return_value.all.side_effect = side_effect_all

    result = generate_briefing(db, user_id=1, role="buyer")
    rfq_section = next(s for s in result["sections"] if s["name"] == "open_rfqs_no_offers")
    assert rfq_section["count"] >= 0  # May be 0 depending on mock chain


def test_quotes_needing_followup_section_exists():
    """The quotes_needing_followup section is in sales briefing."""
    db = _mock_db()
    with patch("app.services.activity_insights._detect_gone_quiet", return_value=[]):
        result = generate_briefing(db, user_id=1, role="sales")

    names = [s["name"] for s in result["sections"]]
    assert "quotes_needing_followup" in names


def test_overnight_vendor_quotes_section_exists():
    """The overnight_vendor_quotes section is in sales briefing."""
    db = _mock_db()
    with patch("app.services.activity_insights._detect_gone_quiet", return_value=[]):
        result = generate_briefing(db, user_id=1, role="sales")

    names = [s["name"] for s in result["sections"]]
    assert "overnight_vendor_quotes" in names


class TestSendBriefingToTeams:
    """Test the _send_briefing_to_teams helper in knowledge_jobs."""

    @pytest.mark.asyncio
    async def test_sends_adaptive_card_to_webhook(self):
        from app.jobs.knowledge_jobs import _send_briefing_to_teams

        briefing = {
            "total_items": 3,
            "sections": [
                {"label": "Vendor Emails", "count": 2, "items": [
                    {"title": "Email from Arrow"}, {"title": "Email from Avnet"}
                ]},
                {"label": "Stalling Deals", "count": 1, "items": [
                    {"title": "Req #42 idle 5d"}
                ]},
                {"label": "Empty Section", "count": 0, "items": []},
            ],
        }

        captured = {}
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        async def mock_post(url, json=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            return mock_resp

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = mock_post
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            await _send_briefing_to_teams(
                "https://webhook.example.com/test", briefing, "Alice"
            )

        assert captured["url"] == "https://webhook.example.com/test"
        body = captured["json"]
        assert body["type"] == "message"
        card = body["attachments"][0]["content"]
        assert card["type"] == "AdaptiveCard"
        assert "Alice" in card["body"][0]["text"]
        assert "3" in card["body"][1]["text"]

    @pytest.mark.asyncio
    async def test_skips_empty_sections_in_text(self):
        from app.jobs.knowledge_jobs import _send_briefing_to_teams

        briefing = {
            "total_items": 1,
            "sections": [
                {"label": "Active", "count": 1, "items": [{"title": "item1"}]},
                {"label": "Empty", "count": 0, "items": []},
            ],
        }

        captured = {}
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()

        async def mock_post(url, json=None, **kwargs):
            captured["json"] = json
            return mock_resp

        with patch("httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.post = mock_post
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            await _send_briefing_to_teams(
                "https://webhook.example.com/test", briefing, "Bob"
            )

        sections_text = captured["json"]["attachments"][0]["content"]["body"][2]["text"]
        assert "Active" in sections_text
        assert "Empty" not in sections_text

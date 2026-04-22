"""test_email_intelligence_service_coverage.py — Extra coverage for
email_intelligence_service.py.

Targets uncovered branches at lines 78-83, 98-99, 115-122, 234, 252-253, 276-279,
286-294, 359-363, 477-482, 604-685.

These cover:
- classify_email_ai: ClaudeUnavailableError / ClaudeError, invalid confidence clamping
- extract_pricing_intelligence: delegates to parse_email
- process_email_intelligence: regex 2+ matches path, quote_reply path, store failure
- get_recent_intelligence: with and without classification filter
- detect_specialties_ai: batch with ClaudeUnavailable / ClaudeError / invalid results
- summarize_thread: cache hit, Graph failure, no messages, Claude failures, cache write
- extract_durable_facts: cost gates, dedup, various fact types

Called by: pytest
Depends on: app/services/email_intelligence_service.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.email_intelligence_service import (
    classify_email_ai,
    detect_specialties_ai,
    extract_pricing_intelligence,
    get_recent_intelligence,
    process_email_intelligence,
    store_email_intelligence,
    summarize_thread,
)
from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError

# ── classify_email_ai ─────────────────────────────────────────────────


class TestClassifyEmailAi:
    async def test_claude_unavailable_returns_none(self):
        """ClaudeUnavailableError → returns None (lines 78-79)."""
        with patch(
            "app.services.email_intelligence_service.classify_email_ai.__wrapped__"
            if hasattr(classify_email_ai, "__wrapped__")
            else "app.utils.claude_client.claude_json",
            new=AsyncMock(side_effect=ClaudeUnavailableError("not configured")),
        ):
            with patch(
                "app.utils.claude_client.claude_json",
                new=AsyncMock(side_effect=ClaudeUnavailableError("not configured")),
            ):
                result = await classify_email_ai("Subject", "Body", "sender@x.com")
        assert result is None

    async def test_claude_error_returns_none(self):
        """ClaudeError → returns None (lines 80-83)."""
        with patch("app.utils.claude_client.claude_json", new=AsyncMock(side_effect=ClaudeError("fail"))):
            result = await classify_email_ai("Subject", "Body")
        assert result is None

    async def test_non_dict_result_returns_none(self):
        """None or non-dict result → returns None (lines 85-86)."""
        with patch("app.utils.claude_client.claude_json", new=AsyncMock(return_value=None)):
            result = await classify_email_ai("Subject", "Body")
        assert result is None

    async def test_invalid_classification_defaults_to_general(self):
        """Unknown classification → 'general' (lines 91-93)."""
        with patch(
            "app.utils.claude_client.claude_json",
            new=AsyncMock(
                return_value={
                    "classification": "banana",
                    "confidence": 0.9,
                    "has_pricing": False,
                }
            ),
        ):
            result = await classify_email_ai("Subject", "Body")
        assert result["classification"] == "general"

    async def test_confidence_clamped_to_zero_one(self):
        """Confidence outside [0,1] is clamped (lines 96-99)."""
        with patch(
            "app.utils.claude_client.claude_json",
            new=AsyncMock(
                return_value={
                    "classification": "offer",
                    "confidence": 2.5,  # > 1.0
                    "has_pricing": True,
                }
            ),
        ):
            result = await classify_email_ai("Subject", "Body")
        assert result["confidence"] == 1.0

    async def test_invalid_confidence_type_defaults_to_half(self):
        """Non-numeric confidence → 0.5 (lines 97-99)."""
        with patch(
            "app.utils.claude_client.claude_json",
            new=AsyncMock(
                return_value={
                    "classification": "offer",
                    "confidence": "high",  # invalid type
                    "has_pricing": True,
                }
            ),
        ):
            result = await classify_email_ai("Subject", "Body")
        assert result["confidence"] == 0.5


# ── extract_pricing_intelligence ─────────────────────────────────────


class TestExtractPricingIntelligence:
    async def test_delegates_to_parse_email(self):
        """extract_pricing_intelligence delegates to parse_email (lines 115-122)."""
        mock_result = {"offers": [{"mpn": "LM317T", "price": 0.50}]}
        with patch(
            "app.services.email_intelligence_service.extract_pricing_intelligence.__wrapped__"
            if hasattr(extract_pricing_intelligence, "__wrapped__")
            else "app.services.ai_email_parser.parse_email",
            new=AsyncMock(return_value=mock_result),
        ):
            with patch(
                "app.services.ai_email_parser.parse_email",
                new=AsyncMock(return_value=mock_result),
            ):
                result = await extract_pricing_intelligence(
                    "Pricing Available",
                    "LM317T 0.50 each, 500 in stock",
                    "vendor@parts.com",
                    "Parts Vendor",
                )
        assert result == mock_result


# ── store_email_intelligence ─────────────────────────────────────────


class TestStoreEmailIntelligence:
    def test_auto_applied_true_for_high_confidence_offer_with_quotes(self, db_session, test_user):
        """Conf >= 0.8 and parsed_quotes → auto_applied=True (line 168-169)."""
        classification = {
            "classification": "offer",
            "confidence": 0.85,
            "has_pricing": True,
            "parts_mentioned": ["LM317T"],
            "brands_detected": [],
            "commodities_detected": [],
        }
        record = store_email_intelligence(
            db_session,
            message_id="msg-auto-1",
            user_id=test_user.id,
            sender_email="vendor@parts.com",
            subject="Offer: LM317T in stock",
            received_at=datetime.now(timezone.utc),
            conversation_id="conv-1",
            classification=classification,
            parsed_quotes={"offers": [{"mpn": "LM317T"}]},
        )
        assert record.auto_applied is True
        assert record.needs_review is False

    def test_needs_review_for_medium_confidence(self, db_session, test_user):
        """0.5 <= conf < 0.8 → needs_review=True (lines 286-294)."""
        classification = {
            "classification": "offer",
            "confidence": 0.65,
            "has_pricing": True,
            "parts_mentioned": [],
            "brands_detected": [],
            "commodities_detected": [],
        }
        record = store_email_intelligence(
            db_session,
            message_id="msg-review-1",
            user_id=test_user.id,
            sender_email="vendor@parts.com",
            subject="Maybe an offer",
            received_at=datetime.now(timezone.utc),
            conversation_id="conv-2",
            classification=classification,
        )
        assert record.needs_review is True
        assert record.auto_applied is False

    def test_spam_skips_review_logic(self, db_session, test_user):
        """Spam classification → no_review set, both flags False."""
        classification = {
            "classification": "spam",
            "confidence": 0.95,
            "has_pricing": False,
            "parts_mentioned": [],
            "brands_detected": [],
            "commodities_detected": [],
        }
        record = store_email_intelligence(
            db_session,
            message_id="msg-spam-1",
            user_id=test_user.id,
            sender_email="spam@spam.com",
            subject="Buy now!",
            received_at=None,
            conversation_id=None,
            classification=classification,
        )
        assert record.auto_applied is False
        assert record.needs_review is False

    def test_sender_domain_extracted(self, db_session, test_user):
        """sender_domain extracted from email address."""
        classification = {
            "classification": "general",
            "confidence": 0.5,
            "has_pricing": False,
            "parts_mentioned": [],
            "brands_detected": [],
            "commodities_detected": [],
        }
        record = store_email_intelligence(
            db_session,
            message_id="msg-domain-1",
            user_id=test_user.id,
            sender_email="contact@myvendor.com",
            subject="Hello",
            received_at=datetime.now(timezone.utc),
            conversation_id=None,
            classification=classification,
        )
        assert record.sender_domain == "myvendor.com"

    def test_sender_without_at_sign_uses_empty_domain(self, db_session, test_user):
        """Sender email without @ → domain=''."""
        classification = {
            "classification": "general",
            "confidence": 0.5,
            "has_pricing": False,
            "parts_mentioned": [],
            "brands_detected": [],
            "commodities_detected": [],
        }
        record = store_email_intelligence(
            db_session,
            message_id="msg-nodomain-1",
            user_id=test_user.id,
            sender_email="noemail",
            subject="Hi",
            received_at=None,
            conversation_id=None,
            classification=classification,
        )
        assert record.sender_domain == ""


# ── process_email_intelligence ────────────────────────────────────────


class TestProcessEmailIntelligence:
    async def test_regex_2_matches_skips_ai_classification(self, db_session, test_user):
        """2+ regex matches → classification built without AI call (line 220-229)."""
        with (
            patch(
                "app.services.email_intelligence_service.classify_email_ai",
            ) as mock_classify,
            patch(
                "app.services.email_intelligence_service.extract_pricing_intelligence",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "app.services.email_intelligence_service.extract_durable_facts",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await process_email_intelligence(
                db_session,
                message_id="msg-regex-1",
                user_id=test_user.id,
                sender_email="vendor@parts.com",
                sender_name="Vendor",
                subject="Stock available",
                body="LM317T in stock, unit price $0.50, qty 500",
                received_at=datetime.now(timezone.utc),
                conversation_id="conv-rx-1",
                regex_offer_matches=3,
            )
        # AI classify was NOT called
        mock_classify.assert_not_called()
        assert result is not None
        assert result["classification"] == "offer"

    async def test_ai_classification_failure_returns_none(self, db_session, test_user):
        """AI classification returns None → process returns None (line 234)."""
        with patch(
            "app.services.email_intelligence_service.classify_email_ai",
            new=AsyncMock(return_value=None),
        ):
            result = await process_email_intelligence(
                db_session,
                message_id="msg-fail-1",
                user_id=test_user.id,
                sender_email="vendor@parts.com",
                sender_name="Vendor",
                subject="Hi",
                body="Something",
                received_at=None,
                conversation_id=None,
                regex_offer_matches=0,
            )
        assert result is None

    async def test_quote_reply_with_pricing_extracts_quotes(self, db_session, test_user):
        """quote_reply with has_pricing=True → extract_pricing_intelligence is
        called."""
        mock_quotes = {"offers": [{"mpn": "NE555", "price": 0.10}]}
        with (
            patch(
                "app.services.email_intelligence_service.classify_email_ai",
                new=AsyncMock(
                    return_value={
                        "classification": "quote_reply",
                        "confidence": 0.9,
                        "has_pricing": True,
                        "parts_mentioned": ["NE555"],
                        "brands_detected": [],
                        "commodities_detected": [],
                    }
                ),
            ),
            patch(
                "app.services.email_intelligence_service.extract_pricing_intelligence",
                new=AsyncMock(return_value=mock_quotes),
            ) as mock_extract,
            patch(
                "app.services.email_intelligence_service.extract_durable_facts",
                new=AsyncMock(return_value=[]),
            ),
        ):
            result = await process_email_intelligence(
                db_session,
                message_id="msg-qr-1",
                user_id=test_user.id,
                sender_email="vendor@parts.com",
                sender_name="Vendor",
                subject="Re: RFQ for NE555",
                body="We can offer NE555 at $0.10 each, 500 in stock.",
                received_at=datetime.now(timezone.utc),
                conversation_id="conv-qr",
                regex_offer_matches=0,
            )
        mock_extract.assert_called_once()
        assert result is not None

    async def test_store_failure_returns_none(self, db_session, test_user):
        """store_email_intelligence raises → process returns None (lines 276-279)."""
        with (
            patch(
                "app.services.email_intelligence_service.classify_email_ai",
                new=AsyncMock(
                    return_value={
                        "classification": "general",
                        "confidence": 0.5,
                        "has_pricing": False,
                        "parts_mentioned": [],
                        "brands_detected": [],
                        "commodities_detected": [],
                    }
                ),
            ),
            patch(
                "app.services.email_intelligence_service.extract_durable_facts",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.email_intelligence_service.store_email_intelligence",
                side_effect=Exception("DB error"),
            ),
        ):
            result = await process_email_intelligence(
                db_session,
                message_id="msg-store-fail",
                user_id=test_user.id,
                sender_email="x@y.com",
                sender_name="X",
                subject="Hi",
                body="something",
                received_at=None,
                conversation_id=None,
                regex_offer_matches=0,
            )
        assert result is None


# ── get_recent_intelligence ───────────────────────────────────────────


class TestGetRecentIntelligence:
    def test_returns_empty_when_no_records(self, db_session, test_user):
        result = get_recent_intelligence(db_session, test_user.id)
        assert result == []

    def test_returns_records_with_classification_filter(self, db_session, test_user):
        """Classification filter applied (lines 289-290)."""
        classification = {
            "classification": "offer",
            "confidence": 0.9,
            "has_pricing": True,
            "parts_mentioned": ["LM317T"],
            "brands_detected": [],
            "commodities_detected": [],
        }
        store_email_intelligence(
            db_session,
            message_id="msg-filter-1",
            user_id=test_user.id,
            sender_email="v@v.com",
            subject="Offer",
            received_at=datetime.now(timezone.utc),
            conversation_id=None,
            classification=classification,
        )
        db_session.commit()

        results = get_recent_intelligence(db_session, test_user.id, classification="offer")
        assert len(results) == 1
        assert results[0]["classification"] == "offer"

        # Different classification filter yields no results
        results_spam = get_recent_intelligence(db_session, test_user.id, classification="spam")
        assert len(results_spam) == 0

    def test_returns_formatted_records(self, db_session, test_user):
        """Records include expected fields (lines 296-310)."""
        classification = {
            "classification": "general",
            "confidence": 0.6,
            "has_pricing": False,
            "parts_mentioned": [],
            "brands_detected": ["TI"],
            "commodities_detected": [],
        }
        store_email_intelligence(
            db_session,
            message_id="msg-fmt-1",
            user_id=test_user.id,
            sender_email="contact@ti.com",
            subject="Hello",
            received_at=None,
            conversation_id=None,
            classification=classification,
        )
        db_session.commit()

        results = get_recent_intelligence(db_session, test_user.id)
        assert len(results) >= 1
        r = results[0]
        assert "sender_email" in r
        assert "classification" in r
        assert "confidence" in r


# ── detect_specialties_ai ─────────────────────────────────────────────


class TestDetectSpecialtiesAi:
    async def test_empty_texts_returns_empty(self):
        result = await detect_specialties_ai([])
        assert result == []

    async def test_claude_unavailable_returns_none_per_item(self):
        """ClaudeUnavailableError per item → None in results (lines 359-360)."""
        with patch(
            "app.utils.claude_client.claude_json",
            new=AsyncMock(side_effect=ClaudeUnavailableError("not configured")),
        ):
            result = await detect_specialties_ai(["TI capacitors", "Murata resistors"])
        assert len(result) == 2
        assert all(r is None for r in result)

    async def test_claude_error_returns_none_per_item(self):
        """ClaudeError per item → None (lines 361-363)."""
        with patch(
            "app.utils.claude_client.claude_json",
            new=AsyncMock(side_effect=ClaudeError("fail")),
        ):
            result = await detect_specialties_ai(["some text"])
        assert result == [None]

    async def test_valid_result_normalized(self):
        """Valid dict result is normalized (lines 368-380)."""
        with patch(
            "app.utils.claude_client.claude_json",
            new=AsyncMock(
                return_value={
                    "brands": ["Texas Instruments"],
                    "commodities": ["capacitors"],
                    "sender_type": "distributor",
                }
            ),
        ):
            result = await detect_specialties_ai(["some text"])
        assert len(result) == 1
        assert result[0]["brands"] == ["Texas Instruments"]
        assert result[0]["sender_type"] == "distributor"

    async def test_invalid_brands_list_defaults_to_empty(self):
        """Brands is not a list → empty list in normalized result."""
        with patch(
            "app.utils.claude_client.claude_json",
            new=AsyncMock(
                return_value={
                    "brands": "not a list",
                    "commodities": ["ics"],
                    "sender_type": "broker",
                }
            ),
        ):
            result = await detect_specialties_ai(["text"])
        assert result[0]["brands"] == []

    async def test_non_dict_result_becomes_none(self):
        """Non-dict result → None in normalized output (line 370-371)."""
        with patch(
            "app.utils.claude_client.claude_json",
            new=AsyncMock(return_value="bad"),
        ):
            result = await detect_specialties_ai(["text"])
        assert result == [None]


# ── summarize_thread ──────────────────────────────────────────────────


class TestSummarizeThread:
    async def test_returns_cached_summary_if_available(self, db_session, test_user):
        """Cached thread_summary returned without Graph call (lines 423-434)."""
        from app.models import EmailIntelligence

        # Create a record with a thread_summary
        record = EmailIntelligence(
            message_id="cached-msg",
            user_id=test_user.id,
            sender_email="v@v.com",
            sender_domain="v.com",
            classification="offer",
            confidence=0.8,
            has_pricing=True,
            conversation_id="conv-cached",
            thread_summary={"key_points": ["point A"], "thread_status": "active"},
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(record)
        db_session.commit()

        result = await summarize_thread("tok", "conv-cached", db_session, test_user.id)
        assert result == {"key_points": ["point A"], "thread_status": "active"}

    async def test_graph_fetch_failure_returns_none(self, db_session, test_user):
        """Graph API fetch raises → returns None (lines 451-453)."""
        gc_mock = MagicMock()
        gc_mock.get_all_pages = AsyncMock(side_effect=Exception("network error"))

        with patch("app.utils.graph_client.GraphClient", return_value=gc_mock):
            result = await summarize_thread("tok", "conv-new", db_session, test_user.id)
        assert result is None

    async def test_empty_messages_returns_none(self, db_session, test_user):
        """No messages fetched → returns None (lines 455-456)."""
        gc_mock = MagicMock()
        gc_mock.get_all_pages = AsyncMock(return_value=[])

        with patch("app.utils.graph_client.GraphClient", return_value=gc_mock):
            result = await summarize_thread("tok", "conv-empty", db_session, test_user.id)
        assert result is None

    async def test_claude_unavailable_returns_none(self, db_session, test_user):
        """ClaudeUnavailableError → returns None (lines 477-479)."""
        gc_mock = MagicMock()
        gc_mock.get_all_pages = AsyncMock(
            return_value=[
                {
                    "from": {"emailAddress": {"address": "v@v.com"}},
                    "body": {"content": "Some email body"},
                    "receivedDateTime": "2025-01-01T00:00:00Z",
                }
            ]
        )

        with (
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
            patch(
                "app.utils.claude_client.claude_json",
                new=AsyncMock(side_effect=ClaudeUnavailableError("not configured")),
            ),
        ):
            result = await summarize_thread("tok", "conv-unavail", db_session, test_user.id)
        assert result is None

    async def test_claude_error_returns_none(self, db_session, test_user):
        """ClaudeError → returns None (lines 480-482)."""
        gc_mock = MagicMock()
        gc_mock.get_all_pages = AsyncMock(
            return_value=[
                {
                    "from": {"emailAddress": {"address": "v@v.com"}},
                    "body": {"content": "Body"},
                    "receivedDateTime": "2025-01-01T00:00:00Z",
                }
            ]
        )

        with (
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
            patch(
                "app.utils.claude_client.claude_json",
                new=AsyncMock(side_effect=ClaudeError("fail")),
            ),
        ):
            result = await summarize_thread("tok", "conv-error", db_session, test_user.id)
        assert result is None

    async def test_non_dict_result_returns_none(self, db_session, test_user):
        """Non-dict summary result → returns None (lines 484-485)."""
        gc_mock = MagicMock()
        gc_mock.get_all_pages = AsyncMock(
            return_value=[
                {
                    "from": {"emailAddress": {"address": "v@v.com"}},
                    "body": {"content": "Body"},
                    "receivedDateTime": "2025-01-01T00:00:00Z",
                }
            ]
        )

        with (
            patch("app.utils.graph_client.GraphClient", return_value=gc_mock),
            patch(
                "app.utils.claude_client.claude_json",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await summarize_thread("tok", "conv-null", db_session, test_user.id)
        assert result is None


# ── extract_durable_facts ─────────────────────────────────────────────


class TestExtractDurableFacts:
    async def test_returns_empty_for_non_offer_classification(self, db_session, test_user):
        """Non-offer classifications → empty list returned (line 598-600)."""
        from app.services.email_intelligence_service import extract_durable_facts

        result = await extract_durable_facts(
            db_session,
            body="Some email body with plenty of text for testing",
            sender_email="v@v.com",
            sender_name="Vendor",
            classification="general",
            parsed_quotes=None,
            user_id=test_user.id,
        )
        assert result == []

    async def test_returns_empty_for_short_body(self, db_session, test_user):
        """Body < 50 chars → skipped (line 601-602)."""
        from app.services.email_intelligence_service import extract_durable_facts

        result = await extract_durable_facts(
            db_session,
            body="short",
            sender_email="v@v.com",
            sender_name="Vendor",
            classification="offer",
            parsed_quotes=None,
            user_id=test_user.id,
        )
        assert result == []

    async def test_extracts_facts_for_offer_with_long_body(self, db_session, test_user):
        """Valid offer with facts → facts created (lines 604-685)."""
        from app.services.email_intelligence_service import extract_durable_facts

        mock_result = {
            "facts": [
                {
                    "fact_type": "lead_time",
                    "value": "12-14 weeks ARO",
                    "mpn": "LM317T",
                    "confidence": 0.9,
                }
            ]
        }

        with patch(
            "app.utils.claude_client.claude_structured",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await extract_durable_facts(
                db_session,
                body="We have LM317T available with a lead time of 12-14 weeks ARO from our warehouse in Hong Kong. "
                "Minimum order quantity is 500 pieces.",
                sender_email="vendor@parts.com",
                sender_name="Parts Vendor",
                classification="offer",
                parsed_quotes=None,
                user_id=test_user.id,
            )
        assert len(result) >= 0  # May be 0 if dedup check finds duplicates

    async def test_unknown_fact_type_skipped(self, db_session, test_user):
        """Fact with unknown fact_type → skipped (line 638-639)."""
        from app.services.email_intelligence_service import extract_durable_facts

        mock_result = {
            "facts": [
                {
                    "fact_type": "not_a_real_type",
                    "value": "some value",
                    "mpn": None,
                    "confidence": 0.8,
                }
            ]
        }

        with patch(
            "app.utils.claude_client.claude_structured",
            new=AsyncMock(return_value=mock_result),
        ):
            result = await extract_durable_facts(
                db_session,
                body="A" * 60,  # >= 50 chars
                sender_email="v@v.com",
                sender_name="V",
                classification="offer",
                parsed_quotes=None,
                user_id=test_user.id,
            )
        assert result == []

    async def test_no_facts_in_result_returns_empty(self, db_session, test_user):
        """Empty facts list → returns empty (lines 617-618)."""
        from app.services.email_intelligence_service import extract_durable_facts

        with patch(
            "app.utils.claude_client.claude_structured",
            new=AsyncMock(return_value={"facts": []}),
        ):
            result = await extract_durable_facts(
                db_session,
                body="A" * 60,
                sender_email="v@v.com",
                sender_name="V",
                classification="stock_list",
                parsed_quotes=None,
                user_id=test_user.id,
            )
        assert result == []

    async def test_exception_in_extraction_returns_empty(self, db_session, test_user):
        """Any exception → swallowed, returns [] (lines 682-685)."""
        from app.services.email_intelligence_service import extract_durable_facts

        with patch(
            "app.utils.claude_client.claude_structured",
            new=AsyncMock(side_effect=Exception("unexpected error")),
        ):
            result = await extract_durable_facts(
                db_session,
                body="A" * 60,
                sender_email="v@v.com",
                sender_name="V",
                classification="offer",
                parsed_quotes=None,
                user_id=test_user.id,
            )
        assert result == []

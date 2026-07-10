"""Tests for Phase 2 — AI-Powered Inbox Mining.

Covers:
  - EmailIntelligence model creation and querying
  - AI email classification (mocked Claude calls)
  - Pricing intelligence extraction
  - store_email_intelligence persistence
  - process_email_intelligence pipeline
  - GET /api/email-intelligence endpoint

Called by: pytest
Depends on: conftest fixtures, app.services.email_intelligence_service
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from tests.conftest import engine  # noqa: F401

# ═══════════════════════════════════════════════════════════════════════
#  EmailIntelligence Model
# ═══════════════════════════════════════════════════════════════════════


class TestEmailIntelligenceModel:
    def test_create_record(self, db_session: Session, test_user):
        """EmailIntelligence record can be created and queried."""
        from app.models import EmailIntelligence

        record = EmailIntelligence(
            message_id="msg-001",
            user_id=test_user.id,
            sender_email="vendor@example.com",
            sender_domain="example.com",
            classification="offer",
            confidence=0.9,
            has_pricing=True,
            parts_detected=["LM317T", "STM32F407"],
            brands_detected=["Texas Instruments"],
            commodities_detected=["voltage regulators"],
            subject="Quote for LM317T",
            received_at=datetime.now(UTC),
            conversation_id="conv-001",
        )
        db_session.add(record)
        db_session.commit()

        fetched = db_session.query(EmailIntelligence).filter_by(message_id="msg-001").first()
        assert fetched is not None
        assert fetched.classification == "offer"
        assert fetched.confidence == 0.9
        assert fetched.has_pricing is True
        assert "LM317T" in fetched.parts_detected

    def test_default_values(self, db_session: Session, test_user):
        """Defaults are applied correctly."""
        from app.models import EmailIntelligence

        record = EmailIntelligence(
            message_id="msg-002",
            user_id=test_user.id,
            sender_email="info@test.com",
            sender_domain="test.com",
            classification="general",
            confidence=0.5,
        )
        db_session.add(record)
        db_session.commit()

        assert record.auto_applied is False
        assert record.needs_review is False
        assert record.has_pricing is False


# ═══════════════════════════════════════════════════════════════════════
#  AI Classification
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyEmailAI:
    @staticmethod
    def _classify_with_mock(mock_return, subject, body, sender):
        """Run classify_email_ai with claude_json mocked to mock_return."""
        from app.services.email_intelligence_service import classify_email_ai

        with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock, return_value=mock_return):
            return asyncio.get_event_loop().run_until_complete(classify_email_ai(subject, body, sender))

    @pytest.mark.parametrize(
        ("mock_return", "args", "expected"),
        [
            pytest.param(
                {
                    "classification": "offer",
                    "confidence": 0.92,
                    "parts_mentioned": ["LM317T"],
                    "has_pricing": True,
                    "brands_detected": ["TI"],
                    "commodities_detected": ["regulators"],
                },
                ("Quote Response", "LM317T available at $0.50", "vendor@test.com"),
                {"classification": "offer", "confidence": 0.92},
                id="offer",
            ),
            pytest.param(
                {
                    "classification": "ooo",
                    "confidence": 0.95,
                    "parts_mentioned": [],
                    "has_pricing": False,
                    "brands_detected": [],
                    "commodities_detected": [],
                },
                ("Out of Office", "I will be out until March 1", "person@test.com"),
                {"classification": "ooo"},
                id="ooo",
            ),
            pytest.param(
                {
                    "classification": "INVALID",
                    "confidence": 0.8,
                    "parts_mentioned": [],
                    "has_pricing": False,
                    "brands_detected": [],
                    "commodities_detected": [],
                },
                ("Test", "Body", "a@b.com"),
                {"classification": "general"},
                id="invalid_class_defaults_general",
            ),
            pytest.param(
                {
                    "classification": "offer",
                    "confidence": 1.5,
                    "parts_mentioned": [],
                    "has_pricing": False,
                    "brands_detected": [],
                    "commodities_detected": [],
                },
                ("Test", "Body", "a@b.com"),
                {"confidence": 1.0},
                id="confidence_clamped",
            ),
        ],
    )
    def test_classify_email_success(self, mock_return, args, expected):
        """AI classification maps/clamps fields from Claude's response."""
        result = self._classify_with_mock(mock_return, *args)
        for key, value in expected.items():
            assert result[key] == value

    def test_classify_email_claude_failure(self):
        """Returns None when Claude API fails."""
        result = self._classify_with_mock(None, "Test", "Body", "a@b.com")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  store_email_intelligence
# ═══════════════════════════════════════════════════════════════════════


class TestStoreEmailIntelligence:
    def test_store_high_confidence_auto_applied(self, db_session, test_user):
        """Confidence >= 0.8 with parsed_quotes sets auto_applied=True."""
        from app.services.email_intelligence_service import store_email_intelligence

        record = store_email_intelligence(
            db_session,
            message_id="msg-auto-1",
            user_id=test_user.id,
            sender_email="vendor@chip.com",
            subject="Quote",
            received_at=datetime.now(UTC),
            conversation_id="conv-1",
            classification={"classification": "offer", "confidence": 0.9, "has_pricing": True},
            parsed_quotes={"quotes": [{"part_number": "LM317T"}]},
        )
        db_session.commit()

        assert record.auto_applied is True
        assert record.needs_review is False

    def test_store_medium_confidence_needs_review(self, db_session, test_user):
        """Confidence 0.5-0.8 sets needs_review=True."""
        from app.services.email_intelligence_service import store_email_intelligence

        record = store_email_intelligence(
            db_session,
            message_id="msg-review-1",
            user_id=test_user.id,
            sender_email="vendor@chip.com",
            subject="Maybe a quote",
            received_at=datetime.now(UTC),
            conversation_id="conv-2",
            classification={"classification": "offer", "confidence": 0.65, "has_pricing": True},
        )
        db_session.commit()

        assert record.auto_applied is False
        assert record.needs_review is True

    def test_store_low_confidence_no_flags(self, db_session, test_user):
        """Confidence < 0.5 sets neither flag."""
        from app.services.email_intelligence_service import store_email_intelligence

        record = store_email_intelligence(
            db_session,
            message_id="msg-low-1",
            user_id=test_user.id,
            sender_email="vendor@chip.com",
            subject="General email",
            received_at=None,
            conversation_id=None,
            classification={"classification": "general", "confidence": 0.3},
        )
        db_session.commit()

        assert record.auto_applied is False
        assert record.needs_review is False


# ═══════════════════════════════════════════════════════════════════════
#  process_email_intelligence pipeline
# ═══════════════════════════════════════════════════════════════════════


class TestProcessEmailIntelligence:
    def test_high_regex_matches_skip_ai(self, db_session, test_user):
        """2+ regex matches skip AI classification and go straight to pricing."""
        from app.services.email_intelligence_service import process_email_intelligence

        with (
            patch(
                "app.services.email_intelligence_service.extract_pricing_intelligence",
                new_callable=AsyncMock,
                return_value=None,
            ) as mock_extract,
            patch("app.services.email_intelligence_service.classify_email_ai", new_callable=AsyncMock) as mock_classify,
        ):
            result = asyncio.get_event_loop().run_until_complete(
                process_email_intelligence(
                    db_session,
                    message_id="msg-regex-1",
                    user_id=test_user.id,
                    sender_email="sales@vendor.com",
                    sender_name="Sales",
                    subject="Quote Response",
                    body="We have LM317T in stock at $0.50",
                    received_at=datetime.now(UTC),
                    conversation_id="conv-3",
                    regex_offer_matches=3,
                )
            )

        # AI classification should NOT be called
        mock_classify.assert_not_called()
        # But pricing extraction SHOULD be called (offer with has_pricing)
        mock_extract.assert_called_once()
        assert result is not None
        assert result["classification"] == "offer"

    def test_low_regex_uses_ai(self, db_session, test_user):
        """0-1 regex matches triggers AI classification."""
        from app.services.email_intelligence_service import process_email_intelligence

        mock_classification = {
            "classification": "general",
            "confidence": 0.8,
            "parts_mentioned": [],
            "has_pricing": False,
            "brands_detected": [],
            "commodities_detected": [],
        }

        with patch(
            "app.services.email_intelligence_service.classify_email_ai",
            new_callable=AsyncMock,
            return_value=mock_classification,
        ):
            result = asyncio.get_event_loop().run_until_complete(
                process_email_intelligence(
                    db_session,
                    message_id="msg-ai-1",
                    user_id=test_user.id,
                    sender_email="info@company.com",
                    sender_name="Info",
                    subject="Hello",
                    body="Just checking in",
                    received_at=datetime.now(UTC),
                    conversation_id="conv-4",
                    regex_offer_matches=0,
                )
            )

        assert result is not None
        assert result["classification"] == "general"


class TestCountOfferMatches:
    def _make_miner(self):
        with patch("app.utils.graph_client.GraphClient"):
            from app.connectors.email_mining import EmailMiner

            miner = EmailMiner("fake-token")
            miner.gc = MagicMock()
        return miner

    def test_clear_offer_high_count(self):
        """Clear offer email matches multiple patterns."""
        miner = self._make_miner()
        count = miner._count_offer_matches("RFQ Response: Quotation", "We have LM317T in stock. Unit price $0.50.")
        assert count >= 2

    def test_general_email_low_count(self):
        """Non-offer email matches few patterns."""
        miner = self._make_miner()
        count = miner._count_offer_matches("Meeting invitation", "Please join the call at 2pm.")
        assert count == 0

"""Tests for Phase 2 — AI-Powered Inbox Mining.

Covers:
  - EmailIntelligence model creation and querying
  - AI email classification (mocked Gradient calls)
  - Pricing intelligence extraction
  - store_email_intelligence persistence
  - process_email_intelligence pipeline
  - GET /api/email-intelligence endpoint

Called by: pytest
Depends on: conftest fixtures, app.services.email_intelligence_service
"""

import asyncio
from datetime import datetime, timezone
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
            received_at=datetime.now(timezone.utc),
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
    def test_classify_email_offer(self):
        """AI correctly classifies an offer email."""
        from app.services.email_intelligence_service import classify_email_ai

        mock_result = {
            "classification": "offer",
            "confidence": 0.92,
            "parts_mentioned": ["LM317T"],
            "has_pricing": True,
            "brands_detected": ["TI"],
            "commodities_detected": ["regulators"],
        }

        with patch("app.services.gradient_service.gradient_json", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(
                classify_email_ai("Quote Response", "LM317T available at $0.50", "vendor@test.com")
            )

        assert result["classification"] == "offer"
        assert result["confidence"] == 0.92

    def test_classify_email_ooo(self):
        """AI classifies out-of-office emails."""
        from app.services.email_intelligence_service import classify_email_ai

        mock_result = {
            "classification": "ooo",
            "confidence": 0.95,
            "parts_mentioned": [],
            "has_pricing": False,
            "brands_detected": [],
            "commodities_detected": [],
        }

        with patch("app.services.gradient_service.gradient_json", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(
                classify_email_ai("Out of Office", "I will be out until March 1", "person@test.com")
            )

        assert result["classification"] == "ooo"

    def test_classify_email_invalid_class_defaults_general(self):
        """Invalid classification string defaults to 'general'."""
        from app.services.email_intelligence_service import classify_email_ai

        mock_result = {
            "classification": "INVALID",
            "confidence": 0.8,
            "parts_mentioned": [],
            "has_pricing": False,
            "brands_detected": [],
            "commodities_detected": [],
        }

        with patch("app.services.gradient_service.gradient_json", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(
                classify_email_ai("Test", "Body", "a@b.com")
            )

        assert result["classification"] == "general"

    def test_classify_email_gradient_failure(self):
        """Returns None when Gradient API fails."""
        from app.services.email_intelligence_service import classify_email_ai

        with patch("app.services.gradient_service.gradient_json", new_callable=AsyncMock, return_value=None):
            result = asyncio.get_event_loop().run_until_complete(
                classify_email_ai("Test", "Body", "a@b.com")
            )

        assert result is None

    def test_classify_email_confidence_clamped(self):
        """Confidence values are clamped to 0.0-1.0."""
        from app.services.email_intelligence_service import classify_email_ai

        mock_result = {
            "classification": "offer",
            "confidence": 1.5,
            "parts_mentioned": [],
            "has_pricing": False,
            "brands_detected": [],
            "commodities_detected": [],
        }

        with patch("app.services.gradient_service.gradient_json", new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.get_event_loop().run_until_complete(
                classify_email_ai("Test", "Body", "a@b.com")
            )

        assert result["confidence"] == 1.0


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
            received_at=datetime.now(timezone.utc),
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
            received_at=datetime.now(timezone.utc),
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

        with patch("app.services.email_intelligence_service.extract_pricing_intelligence", new_callable=AsyncMock, return_value=None) as mock_extract, \
             patch("app.services.email_intelligence_service.classify_email_ai", new_callable=AsyncMock) as mock_classify:
            result = asyncio.get_event_loop().run_until_complete(
                process_email_intelligence(
                    db_session,
                    message_id="msg-regex-1",
                    user_id=test_user.id,
                    sender_email="sales@vendor.com",
                    sender_name="Sales",
                    subject="Quote Response",
                    body="We have LM317T in stock at $0.50",
                    received_at=datetime.now(timezone.utc),
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

        with patch("app.services.email_intelligence_service.classify_email_ai", new_callable=AsyncMock, return_value=mock_classification):
            result = asyncio.get_event_loop().run_until_complete(
                process_email_intelligence(
                    db_session,
                    message_id="msg-ai-1",
                    user_id=test_user.id,
                    sender_email="info@company.com",
                    sender_name="Info",
                    subject="Hello",
                    body="Just checking in",
                    received_at=datetime.now(timezone.utc),
                    conversation_id="conv-4",
                    regex_offer_matches=0,
                )
            )

        assert result is not None
        assert result["classification"] == "general"


# ═══════════════════════════════════════════════════════════════════════
#  GET /api/email-intelligence endpoint
# ═══════════════════════════════════════════════════════════════════════


class TestEmailIntelligenceEndpoint:
    def test_list_intelligence(self, client, db_session, test_user):
        """GET /api/email-intelligence returns recent records."""
        from app.models import EmailIntelligence

        # Create test records
        for i in range(3):
            db_session.add(EmailIntelligence(
                message_id=f"msg-ep-{i}",
                user_id=test_user.id,
                sender_email=f"vendor{i}@test.com",
                sender_domain="test.com",
                classification="offer" if i == 0 else "general",
                confidence=0.8,
            ))
        db_session.commit()

        resp = client.get("/api/email-intelligence")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 3
        assert len(data["items"]) == 3

    def test_list_intelligence_with_filter(self, client, db_session, test_user):
        """GET /api/email-intelligence?classification=offer filters results."""
        from app.models import EmailIntelligence

        db_session.add(EmailIntelligence(
            message_id="msg-f-1", user_id=test_user.id,
            sender_email="v@t.com", sender_domain="t.com",
            classification="offer", confidence=0.9,
        ))
        db_session.add(EmailIntelligence(
            message_id="msg-f-2", user_id=test_user.id,
            sender_email="v@t.com", sender_domain="t.com",
            classification="general", confidence=0.7,
        ))
        db_session.commit()

        resp = client.get("/api/email-intelligence?classification=offer")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["items"][0]["classification"] == "offer"

    def test_list_intelligence_empty(self, client):
        """GET /api/email-intelligence returns empty when no records."""
        resp = client.get("/api/email-intelligence")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["items"] == []


# ═══════════════════════════════════════════════════════════════════════
#  _count_offer_matches on EmailMiner
# ═══════════════════════════════════════════════════════════════════════


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
        count = miner._count_offer_matches(
            "RFQ Response: Quotation",
            "We have LM317T in stock. Unit price $0.50."
        )
        assert count >= 2

    def test_general_email_low_count(self):
        """Non-offer email matches few patterns."""
        miner = self._make_miner()
        count = miner._count_offer_matches(
            "Meeting invitation",
            "Please join the call at 2pm."
        )
        assert count == 0

    def test_is_offer_email_uses_count(self):
        """_is_offer_email delegates to _count_offer_matches."""
        miner = self._make_miner()
        assert miner._is_offer_email("RFQ Response: Quotation", "in stock, unit price")
        assert not miner._is_offer_email("Meeting", "Join the call")

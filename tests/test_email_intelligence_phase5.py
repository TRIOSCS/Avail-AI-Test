"""Tests for Phase 5 — Response Analytics + Vendor Email Health.

Covers:
  5A: Response time metrics (compute_vendor_response_metrics)
  5B: Email health score (compute_email_health_score, update_vendor_email_health)
  5C: Contact intelligence integration (avg_response_hours wiring)
  5D: Dashboard endpoint (GET /api/email-intelligence/dashboard)

Called by: pytest
Depends on: conftest fixtures
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from tests.conftest import engine  # noqa: F401


# ═══════════════════════════════════════════════════════════════════════
#  5A: Response Time Metrics
# ═══════════════════════════════════════════════════════════════════════


class TestVendorResponseMetrics:
    def test_metrics_with_responses(self, db_session, test_vendor_card):
        """Computes avg response hours and response rate from VendorResponse records."""
        from app.models.offers import VendorResponse
        from app.services.response_analytics import compute_vendor_response_metrics

        now = datetime.now(timezone.utc)

        # Create VendorResponse records for the vendor domain
        for i in range(3):
            db_session.add(VendorResponse(
                vendor_name=test_vendor_card.display_name,
                vendor_email=f"sales@{test_vendor_card.domain}",
                subject=f"RE: RFQ {i}",
                received_at=now - timedelta(hours=i * 10),
                created_at=now - timedelta(hours=i * 10 + 8),  # 8 hours response time
                classification="offer",
                confidence=0.9,
                parsed_data={"quotes": [{"part": "LM317T"}]} if i < 2 else None,
            ))

        # Set outreach count
        test_vendor_card.total_outreach = 5
        db_session.commit()

        result = compute_vendor_response_metrics(db_session, test_vendor_card.id)

        assert result["response_count"] == 3
        assert result["outreach_count"] == 5
        assert result["response_rate"] == 0.6  # 3/5
        assert result["avg_response_hours"] is not None
        assert result["quote_quality_rate"] > 0  # 2 of 3 had pricing

    def test_metrics_no_vendor(self, db_session):
        """Returns empty metrics for nonexistent vendor."""
        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, 99999)

        assert result["response_count"] == 0
        assert result["avg_response_hours"] is None
        assert result["response_rate"] == 0.0

    def test_metrics_no_responses(self, db_session, test_vendor_card):
        """Returns zero metrics when vendor has no responses."""
        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, test_vendor_card.id)

        assert result["response_count"] == 0
        assert result["avg_response_hours"] is None
        assert result["quote_quality_rate"] == 0.0


# ═══════════════════════════════════════════════════════════════════════
#  5B: Email Health Score
# ═══════════════════════════════════════════════════════════════════════


class TestEmailHealthScore:
    def test_health_score_computation(self, db_session, test_vendor_card):
        """Composite health score is computed correctly."""
        from app.services.response_analytics import compute_email_health_score

        # Set up some data so metrics aren't all zero
        test_vendor_card.total_outreach = 10
        db_session.commit()

        result = compute_email_health_score(db_session, test_vendor_card.id)

        assert "email_health_score" in result
        assert 0.0 <= result["email_health_score"] <= 100.0
        assert "response_rate_score" in result
        assert "response_time_score" in result
        assert "quote_quality_score" in result
        assert "ooo_score" in result
        assert "thread_resolution_score" in result

    def test_health_score_no_vendor(self, db_session):
        """Returns empty metrics for nonexistent vendor."""
        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, 99999)
        assert result["email_health_score"] >= 0

    def test_health_score_ooo_penalty(self, db_session, test_vendor_card):
        """OOO contacts reduce the OOO component score."""
        from app.models import VendorContact
        from app.services.response_analytics import compute_email_health_score

        # Add 2 contacts, 1 OOO
        db_session.add(VendorContact(
            vendor_card_id=test_vendor_card.id,
            full_name="Active Rep",
            email="active@test.com",
            source="manual",
            is_ooo=False,
        ))
        db_session.add(VendorContact(
            vendor_card_id=test_vendor_card.id,
            full_name="OOO Rep",
            email="ooo@test.com",
            source="manual",
            is_ooo=True,
        ))
        db_session.commit()

        result = compute_email_health_score(db_session, test_vendor_card.id)

        # OOO score should be 50 (1 of 2 contacts is OOO)
        assert result["ooo_score"] == 50.0

    def test_update_vendor_email_health(self, db_session, test_vendor_card):
        """update_vendor_email_health persists score on VendorCard."""
        from app.services.response_analytics import update_vendor_email_health

        result = update_vendor_email_health(db_session, test_vendor_card.id)

        assert result is not None
        assert test_vendor_card.email_health_score is not None
        assert test_vendor_card.email_health_computed_at is not None

    def test_update_vendor_email_health_missing(self, db_session):
        """Returns None for nonexistent vendor."""
        from app.services.response_analytics import update_vendor_email_health

        result = update_vendor_email_health(db_session, 99999)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════
#  5B: Batch Update
# ═══════════════════════════════════════════════════════════════════════


class TestBatchEmailHealth:
    def test_batch_update(self, db_session, test_vendor_card):
        """Batch update processes vendors with recent activity."""
        from app.services.response_analytics import batch_update_email_health

        test_vendor_card.last_contact_at = datetime.now(timezone.utc) - timedelta(days=5)
        db_session.commit()

        result = batch_update_email_health(db_session, lookback_days=90)

        assert result["updated"] >= 1
        assert result["errors"] == 0

    def test_batch_update_no_active_vendors(self, db_session):
        """No updates when no vendors have recent activity."""
        from app.services.response_analytics import batch_update_email_health

        result = batch_update_email_health(db_session, lookback_days=1)
        assert result["updated"] == 0


# ═══════════════════════════════════════════════════════════════════════
#  5C: Contact Intelligence Integration
# ═══════════════════════════════════════════════════════════════════════


class TestContactIntelligenceIntegration:
    def test_response_hours_wired_into_scoring(self, db_session, test_vendor_card):
        """compute_all_contact_scores uses avg_response_hours from VendorCard."""
        from app.services.contact_intelligence import compute_contact_relationship_score

        # Verify that when avg_response_hours is provided, it affects the score
        result_with = compute_contact_relationship_score(
            last_interaction_at=datetime.now(timezone.utc),
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=2.0,  # Fast responder
            wins=3,
            total_interactions=10,
            distinct_channels=2,
        )

        result_without = compute_contact_relationship_score(
            last_interaction_at=datetime.now(timezone.utc),
            interactions_30d=5,
            interactions_60d=10,
            interactions_90d=15,
            avg_response_hours=None,  # Unknown
            wins=3,
            total_interactions=10,
            distinct_channels=2,
        )

        # Fast responder should score higher than unknown
        assert result_with["responsiveness_score"] == 100.0
        assert result_without["responsiveness_score"] == 50.0
        assert result_with["relationship_score"] > result_without["relationship_score"]


# ═══════════════════════════════════════════════════════════════════════
#  VendorCard Email Health Columns
# ═══════════════════════════════════════════════════════════════════════


class TestVendorCardHealthColumns:
    def test_email_health_columns_exist(self, db_session, test_vendor_card):
        """VendorCard has email_health_score and related columns."""
        test_vendor_card.email_health_score = 75.5
        test_vendor_card.email_health_computed_at = datetime.now(timezone.utc)
        test_vendor_card.response_rate = 0.65
        test_vendor_card.quote_quality_rate = 0.80
        db_session.commit()

        from app.models import VendorCard
        fetched = db_session.query(VendorCard).get(test_vendor_card.id)
        assert fetched.email_health_score == 75.5
        assert fetched.response_rate == 0.65
        assert fetched.quote_quality_rate == 0.80
        assert fetched.email_health_computed_at is not None


# ═══════════════════════════════════════════════════════════════════════
#  5D: Dashboard Endpoint
# ═══════════════════════════════════════════════════════════════════════


class TestDashboardEndpoint:
    def test_dashboard_empty(self, client):
        """Dashboard returns zeros when no intelligence data exists."""
        resp = client.get("/api/email-intelligence/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["emails_scanned_7d"] == 0
        assert data["offers_detected_7d"] == 0
        assert data["pending_review"] == 0
        assert data["top_vendors"] == []

    def test_dashboard_with_data(self, client, db_session, test_user):
        """Dashboard aggregates intelligence data correctly."""
        from app.models import EmailIntelligence

        now = datetime.now(timezone.utc)

        # Create test intelligence records
        db_session.add(EmailIntelligence(
            message_id="dash-1", user_id=test_user.id,
            sender_email="v@t.com", sender_domain="t.com",
            classification="offer", confidence=0.9,
            has_pricing=True, created_at=now,
        ))
        db_session.add(EmailIntelligence(
            message_id="dash-2", user_id=test_user.id,
            sender_email="v@t.com", sender_domain="t.com",
            classification="general", confidence=0.7,
            needs_review=True, created_at=now,
        ))
        db_session.add(EmailIntelligence(
            message_id="dash-3", user_id=test_user.id,
            sender_email="v@t.com", sender_domain="t.com",
            classification="stock_list", confidence=0.85,
            created_at=now,
        ))
        db_session.commit()

        resp = client.get("/api/email-intelligence/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["emails_scanned_7d"] == 3
        assert data["offers_detected_7d"] == 1
        assert data["stock_lists_7d"] == 1
        assert data["pending_review"] == 1

    def test_dashboard_custom_days(self, client, db_session, test_user):
        """Dashboard respects custom days parameter."""
        from app.models import EmailIntelligence

        # Create record 20 days ago
        old_date = datetime.now(timezone.utc) - timedelta(days=20)
        db_session.add(EmailIntelligence(
            message_id="dash-old", user_id=test_user.id,
            sender_email="v@t.com", sender_domain="t.com",
            classification="offer", confidence=0.9,
            created_at=old_date,
        ))
        db_session.commit()

        # 7-day window should miss it
        resp7 = client.get("/api/email-intelligence/dashboard?days=7")
        assert resp7.json()["offers_detected_7d"] == 0

        # 30-day window should include it
        resp30 = client.get("/api/email-intelligence/dashboard?days=30")
        assert resp30.json()["offers_detected_7d"] == 1

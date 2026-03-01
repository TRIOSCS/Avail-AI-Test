"""Tests for Phase 5 — Response Analytics + Vendor Email Health.

Covers:
  5A: Response time metrics (compute_vendor_response_metrics)
  5B: Email health score (compute_email_health_score, update_vendor_email_health)
  5C: Contact intelligence integration (avg_response_hours wiring)
  5D: Dashboard endpoint (GET /api/email-intelligence/dashboard)

Called by: pytest
Depends on: conftest fixtures
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
            db_session.add(
                VendorResponse(
                    vendor_name=test_vendor_card.display_name,
                    vendor_email=f"sales@{test_vendor_card.domain}",
                    subject=f"RE: RFQ {i}",
                    received_at=now - timedelta(hours=i * 10),
                    created_at=now - timedelta(hours=i * 10 + 8),  # 8 hours response time
                    classification="offer",
                    confidence=0.9,
                    parsed_data={"quotes": [{"part": "LM317T"}]} if i < 2 else None,
                )
            )

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
        db_session.add(
            VendorContact(
                vendor_card_id=test_vendor_card.id,
                full_name="Active Rep",
                email="active@test.com",
                source="manual",
                is_ooo=False,
            )
        )
        db_session.add(
            VendorContact(
                vendor_card_id=test_vendor_card.id,
                full_name="OOO Rep",
                email="ooo@test.com",
                source="manual",
                is_ooo=True,
            )
        )
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
        db_session.add(
            EmailIntelligence(
                message_id="dash-1",
                user_id=test_user.id,
                sender_email="v@t.com",
                sender_domain="t.com",
                classification="offer",
                confidence=0.9,
                has_pricing=True,
                created_at=now,
            )
        )
        db_session.add(
            EmailIntelligence(
                message_id="dash-2",
                user_id=test_user.id,
                sender_email="v@t.com",
                sender_domain="t.com",
                classification="general",
                confidence=0.7,
                needs_review=True,
                created_at=now,
            )
        )
        db_session.add(
            EmailIntelligence(
                message_id="dash-3",
                user_id=test_user.id,
                sender_email="v@t.com",
                sender_domain="t.com",
                classification="stock_list",
                confidence=0.85,
                created_at=now,
            )
        )
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
        db_session.add(
            EmailIntelligence(
                message_id="dash-old",
                user_id=test_user.id,
                sender_email="v@t.com",
                sender_domain="t.com",
                classification="offer",
                confidence=0.9,
                created_at=old_date,
            )
        )
        db_session.commit()

        # 7-day window should miss it
        resp7 = client.get("/api/email-intelligence/dashboard?days=7")
        assert resp7.json()["offers_detected_7d"] == 0

        # 30-day window should include it
        resp30 = client.get("/api/email-intelligence/dashboard?days=30")
        assert resp30.json()["offers_detected_7d"] == 1


# ═══════════════════════════════════════════════════════════════════════
#  Coverage Gap: vendor_name filter when vendor has no domain (line 83, 87-89)
# ═══════════════════════════════════════════════════════════════════════


class TestVendorResponseMetricsNoDomain:
    def test_metrics_no_domain_uses_vendor_name(self, db_session):
        """When vendor has no domain, responses are matched by vendor_name."""
        from app.models import VendorCard
        from app.models.offers import VendorResponse
        from app.services.response_analytics import compute_vendor_response_metrics

        now = datetime.now(timezone.utc)

        # Vendor with NO domain set
        vendor = VendorCard(
            normalized_name="no domain vendor",
            display_name="No Domain Vendor",
            domain=None,
            emails=[],
            sighting_count=1,
            total_outreach=5,
            created_at=now,
        )
        db_session.add(vendor)
        db_session.flush()

        # Responses matched by vendor_name (not email domain)
        for i in range(2):
            db_session.add(
                VendorResponse(
                    vendor_name="No Domain Vendor",
                    vendor_email=f"rep{i}@unknown.com",
                    subject=f"RE: RFQ {i}",
                    received_at=now - timedelta(hours=i * 5),
                    created_at=now - timedelta(hours=i * 5 + 6),
                    classification="offer",
                    confidence=0.9,
                )
            )
        db_session.commit()

        result = compute_vendor_response_metrics(db_session, vendor.id)

        assert result["response_count"] == 2
        assert result["outreach_count"] == 5
        assert result["response_rate"] == 0.4  # 2/5


# ═══════════════════════════════════════════════════════════════════════
#  Coverage Gap: median with even count (line 121)
# ═══════════════════════════════════════════════════════════════════════


class TestMedianEvenCount:
    def test_median_even_count(self, db_session):
        """Median is average of two middle values when response count is even."""
        from app.models import VendorCard
        from app.models.offers import VendorResponse
        from app.services.response_analytics import compute_vendor_response_metrics

        now = datetime.now(timezone.utc)

        vendor = VendorCard(
            normalized_name="median test vendor",
            display_name="Median Test Vendor",
            domain="mediantest.com",
            emails=["sales@mediantest.com"],
            sighting_count=1,
            total_outreach=10,
            created_at=now,
        )
        db_session.add(vendor)
        db_session.flush()

        # 4 responses with known response times: 2h, 4h, 6h, 8h
        # Median of even count = (4+6)/2 = 5.0
        response_times = [2, 4, 6, 8]
        for i, hours in enumerate(response_times):
            db_session.add(
                VendorResponse(
                    vendor_name="Median Test Vendor",
                    vendor_email=f"rep{i}@mediantest.com",
                    subject=f"RE: RFQ {i}",
                    received_at=now - timedelta(hours=i * 20),
                    created_at=now - timedelta(hours=i * 20 + hours),
                    classification="offer",
                    confidence=0.9,
                )
            )
        db_session.commit()

        result = compute_vendor_response_metrics(db_session, vendor.id)

        assert result["response_count"] == 4
        assert result["median_response_hours"] == 5.0  # (4+6)/2
        assert result["avg_response_hours"] == 5.0  # (2+4+6+8)/4


# ═══════════════════════════════════════════════════════════════════════
#  Coverage Gap: response time interpolation (lines 182-188)
# ═══════════════════════════════════════════════════════════════════════


class TestResponseTimeInterpolation:
    def test_response_time_between_ideal_and_max(self, db_session):
        """Response time between 4h and 168h yields interpolated score."""
        from app.models import VendorCard
        from app.models.offers import VendorResponse
        from app.services.response_analytics import (
            RESPONSE_IDEAL_HOURS,
            RESPONSE_MAX_HOURS,
            compute_email_health_score,
        )

        now = datetime.now(timezone.utc)

        vendor = VendorCard(
            normalized_name="interp vendor",
            display_name="Interp Vendor",
            domain="interp.com",
            emails=["sales@interp.com"],
            sighting_count=1,
            total_outreach=2,
            created_at=now,
        )
        db_session.add(vendor)
        db_session.flush()

        # Response with 48h response time (between 4h ideal and 168h max)
        db_session.add(
            VendorResponse(
                vendor_name="Interp Vendor",
                vendor_email="sales@interp.com",
                subject="RE: RFQ 1",
                received_at=now,
                created_at=now - timedelta(hours=48),
                classification="offer",
                confidence=0.9,
            )
        )
        db_session.commit()

        result = compute_email_health_score(db_session, vendor.id)

        # Expected: 100 * (1 - (48-4)/(168-4)) = 100 * (1 - 44/164) ≈ 73.2
        expected_time_score = 100.0 * (
            1.0 - (48.0 - RESPONSE_IDEAL_HOURS) / (RESPONSE_MAX_HOURS - RESPONSE_IDEAL_HOURS)
        )
        assert result["response_time_score"] == round(expected_time_score, 1)
        # Verify it's between 0 and 100 (interpolated, not at boundaries)
        assert 0 < result["response_time_score"] < 100

    def test_response_time_at_or_below_ideal(self, db_session):
        """Response time <= 4h gives perfect 100.0 score (line 184)."""
        from app.models import VendorCard
        from app.models.offers import VendorResponse
        from app.services.response_analytics import compute_email_health_score

        now = datetime.now(timezone.utc)

        vendor = VendorCard(
            normalized_name="fast vendor",
            display_name="Fast Vendor",
            domain="fast.com",
            emails=["sales@fast.com"],
            sighting_count=1,
            total_outreach=2,
            created_at=now,
        )
        db_session.add(vendor)
        db_session.flush()

        # Response with 2h response time (<= 4h ideal)
        db_session.add(
            VendorResponse(
                vendor_name="Fast Vendor",
                vendor_email="sales@fast.com",
                subject="RE: RFQ 1",
                received_at=now,
                created_at=now - timedelta(hours=2),
                classification="offer",
                confidence=0.9,
            )
        )
        db_session.commit()

        result = compute_email_health_score(db_session, vendor.id)
        assert result["response_time_score"] == 100.0

    def test_response_time_at_or_above_max(self, db_session):
        """Response time >= 168h gives 0.0 score (line 186)."""
        from app.models import VendorCard
        from app.models.offers import VendorResponse
        from app.services.response_analytics import compute_email_health_score

        now = datetime.now(timezone.utc)

        vendor = VendorCard(
            normalized_name="slow vendor",
            display_name="Slow Vendor",
            domain="slow.com",
            emails=["sales@slow.com"],
            sighting_count=1,
            total_outreach=2,
            created_at=now,
        )
        db_session.add(vendor)
        db_session.flush()

        # Response with 200h response time (>= 168h max)
        db_session.add(
            VendorResponse(
                vendor_name="Slow Vendor",
                vendor_email="sales@slow.com",
                subject="RE: RFQ 1",
                received_at=now,
                created_at=now - timedelta(hours=200),
                classification="offer",
                confidence=0.9,
            )
        )
        db_session.commit()

        result = compute_email_health_score(db_session, vendor.id)
        assert result["response_time_score"] == 0.0


# ═══════════════════════════════════════════════════════════════════════
#  Coverage Gap: thread resolution scoring (lines 228-254)
# ═══════════════════════════════════════════════════════════════════════


class TestThreadResolutionScoring:
    def test_thread_resolution_with_closed_threads(self, db_session, test_user):
        """Thread resolution score counts closed/quoted threads correctly."""
        from app.models import EmailIntelligence, VendorCard
        from app.services.response_analytics import compute_email_health_score

        now = datetime.now(timezone.utc)

        vendor = VendorCard(
            normalized_name="thread vendor",
            display_name="Thread Vendor",
            domain="threadvendor.com",
            emails=["sales@threadvendor.com"],
            sighting_count=1,
            total_outreach=5,
            created_at=now,
        )
        db_session.add(vendor)
        db_session.flush()

        # 4 threads: 2 closed, 1 quoted, 1 open → 3/4 = 75% resolution
        thread_statuses = [
            {"thread_status": "closed"},
            {"thread_status": "closed"},
            {"thread_status": "quoted"},
            {"thread_status": "open"},
        ]
        for i, summary in enumerate(thread_statuses):
            db_session.add(
                EmailIntelligence(
                    message_id=f"thread-{i}",
                    user_id=test_user.id,
                    sender_email="rep@threadvendor.com",
                    sender_domain="threadvendor.com",
                    classification="offer",
                    confidence=0.9,
                    thread_summary=summary,
                    created_at=now,
                )
            )
        db_session.commit()

        result = compute_email_health_score(db_session, vendor.id)

        # 3 of 4 threads resolved → 75.0
        assert result["thread_resolution_score"] == 75.0

    def test_thread_resolution_non_dict_summary_skipped(self, db_session, test_user):
        """Non-dict thread_summary entries are skipped (not counted as resolved)."""
        from app.models import EmailIntelligence, VendorCard
        from app.services.response_analytics import compute_email_health_score

        now = datetime.now(timezone.utc)

        vendor = VendorCard(
            normalized_name="nondict vendor",
            display_name="NonDict Vendor",
            domain="nondict.com",
            emails=["sales@nondict.com"],
            sighting_count=1,
            created_at=now,
        )
        db_session.add(vendor)
        db_session.flush()

        # 2 threads: 1 with a dict (closed), 1 with a string (not a dict)
        db_session.add(
            EmailIntelligence(
                message_id="nd-1",
                user_id=test_user.id,
                sender_email="rep@nondict.com",
                sender_domain="nondict.com",
                classification="offer",
                confidence=0.9,
                thread_summary={"thread_status": "closed"},
                created_at=now,
            )
        )
        db_session.add(
            EmailIntelligence(
                message_id="nd-2",
                user_id=test_user.id,
                sender_email="rep@nondict.com",
                sender_domain="nondict.com",
                classification="offer",
                confidence=0.9,
                thread_summary="just a string",
                created_at=now,
            )
        )
        db_session.commit()

        result = compute_email_health_score(db_session, vendor.id)

        # 1 of 2 resolved → 50.0
        assert result["thread_resolution_score"] == 50.0


# ═══════════════════════════════════════════════════════════════════════
#  Coverage Gap: avg_response_hours updated on vendor (line 298)
# ═══════════════════════════════════════════════════════════════════════


class TestUpdateVendorAvgResponseHours:
    def test_update_persists_avg_response_hours(self, db_session):
        """update_vendor_email_health sets avg_response_hours on VendorCard."""
        from app.models import VendorCard
        from app.models.offers import VendorResponse
        from app.services.response_analytics import update_vendor_email_health

        now = datetime.now(timezone.utc)

        vendor = VendorCard(
            normalized_name="avg hrs vendor",
            display_name="Avg Hrs Vendor",
            domain="avghrs.com",
            emails=["sales@avghrs.com"],
            sighting_count=1,
            total_outreach=3,
            created_at=now,
        )
        db_session.add(vendor)
        db_session.flush()

        # Add response with 12h response time
        db_session.add(
            VendorResponse(
                vendor_name="Avg Hrs Vendor",
                vendor_email="sales@avghrs.com",
                subject="RE: RFQ 1",
                received_at=now,
                created_at=now - timedelta(hours=12),
                classification="offer",
                confidence=0.9,
            )
        )
        db_session.commit()

        result = update_vendor_email_health(db_session, vendor.id)

        assert result is not None
        assert vendor.avg_response_hours == 12.0
        assert vendor.email_health_score is not None
        assert vendor.response_rate is not None


# ═══════════════════════════════════════════════════════════════════════
#  Coverage Gap: batch_update exception in loop (lines 333-335)
# ═══════════════════════════════════════════════════════════════════════


class TestBatchUpdateErrors:
    def test_batch_update_individual_error(self, db_session):
        """Individual vendor update failure increments errors count."""
        from app.models import VendorCard
        from app.services.response_analytics import batch_update_email_health

        now = datetime.now(timezone.utc)

        vendor = VendorCard(
            normalized_name="error vendor",
            display_name="Error Vendor",
            domain="error.com",
            emails=[],
            sighting_count=1,
            last_contact_at=now - timedelta(days=5),
            created_at=now,
        )
        db_session.add(vendor)
        db_session.commit()

        with patch(
            "app.services.response_analytics.update_vendor_email_health",
            side_effect=RuntimeError("DB error"),
        ):
            result = batch_update_email_health(db_session, lookback_days=90)

        assert result["errors"] == 1
        assert result["updated"] == 0

    def test_batch_update_commit_failure(self, db_session):
        """Commit failure triggers rollback and returns error info."""
        from app.models import VendorCard
        from app.services.response_analytics import batch_update_email_health

        now = datetime.now(timezone.utc)

        vendor = VendorCard(
            normalized_name="commit fail vendor",
            display_name="Commit Fail Vendor",
            domain="commitfail.com",
            emails=[],
            sighting_count=1,
            last_contact_at=now - timedelta(days=5),
            created_at=now,
        )
        db_session.add(vendor)
        db_session.commit()

        # The update succeeds (updated=1) but commit fails
        with patch.object(db_session, "commit", side_effect=RuntimeError("Commit failed")):
            with patch.object(db_session, "rollback") as mock_rollback:
                result = batch_update_email_health(db_session, lookback_days=90)

        assert result["updated"] >= 1
        mock_rollback.assert_called_once()

"""test_response_analytics.py — Comprehensive tests for
app/services/response_analytics.py.

Covers: compute_vendor_response_metrics, compute_email_health_score,
update_vendor_email_health, batch_update_email_health, get_email_intelligence_dashboard.

Called by: pytest
Depends on: app.services.response_analytics, conftest fixtures
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.models import ActivityLog, VendorCard, VendorContact
from app.models.email_intelligence import EmailIntelligence
from app.models.offers import VendorResponse

# ── Helpers ────────────────────────────────────────────────────────────


def _make_vendor_card(
    db,
    display_name="Test Vendor",
    domain="testvendor.com",
    total_outreach=0,
    last_contact_at=None,
    email_health_score=None,
    avg_response_hours=None,
    response_rate=None,
    quote_quality_rate=None,
):
    """Create a VendorCard in the test DB."""
    card = VendorCard(
        normalized_name=display_name.lower().replace(" ", "_"),
        display_name=display_name,
        domain=domain,
        emails=[f"sales@{domain}"] if domain else [],
        total_outreach=total_outreach,
        last_contact_at=last_contact_at or datetime.now(timezone.utc),
        email_health_score=email_health_score,
        avg_response_hours=avg_response_hours,
        response_rate=response_rate,
        quote_quality_rate=quote_quality_rate,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _make_activity_log(db, user_id, vendor_card_id, activity_type="rfq_sent", days_ago=0):
    """Create an ActivityLog entry."""
    log = ActivityLog(
        user_id=user_id,
        vendor_card_id=vendor_card_id,
        activity_type=activity_type,
        channel="email",
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(log)
    db.commit()
    return log


def _make_vendor_response(
    db,
    vendor_email="sales@testvendor.com",
    vendor_name="Test Vendor",
    received_at=None,
    created_at=None,
    parsed_data=None,
    days_ago=0,
):
    """Create a VendorResponse entry."""
    now = datetime.now(timezone.utc)
    resp = VendorResponse(
        vendor_email=vendor_email,
        vendor_name=vendor_name,
        received_at=received_at or (now - timedelta(days=days_ago)),
        created_at=created_at or (now - timedelta(days=days_ago, hours=4)),
        parsed_data=parsed_data,
    )
    db.add(resp)
    db.commit()
    return resp


# ── compute_vendor_response_metrics() ──────────────────────────────────


class TestComputeVendorResponseMetrics:
    """Tests for compute_vendor_response_metrics."""

    def test_vendor_not_found(self, db_session):
        """Returns empty metrics for non-existent vendor."""
        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, 99999)
        assert result["avg_response_hours"] is None
        assert result["response_rate"] == 0.0
        assert result["response_count"] == 0

    def test_no_outreach_or_responses(self, db_session):
        """Returns zeros when vendor has no activity."""
        card = _make_vendor_card(db_session)
        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["outreach_count"] == 0
        assert result["response_count"] == 0
        assert result["response_rate"] == 0.0

    def test_uses_total_outreach_fallback(self, db_session):
        """Falls back to vendor.total_outreach when no ActivityLog entries."""
        card = _make_vendor_card(db_session, total_outreach=10)
        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["outreach_count"] == 10

    def test_counts_outreach_from_activity_log(self, db_session, test_user):
        """Counts rfq_sent and email_sent activities as outreach."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        _make_activity_log(db_session, test_user.id, card.id, "email_sent")

        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["outreach_count"] == 2

    def test_response_rate_calculation(self, db_session, test_user):
        """Response rate = responses / outreach, capped at 1.0."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        _make_vendor_response(db_session, vendor_email="sales@testvendor.com")

        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["response_rate"] == 0.5
        assert result["response_count"] == 1

    def test_response_rate_capped_at_1(self, db_session, test_user):
        """Response rate is capped at 1.0 even if responses > outreach."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        _make_vendor_response(db_session, vendor_email="sales@testvendor.com", days_ago=0)
        _make_vendor_response(db_session, vendor_email="sales@testvendor.com", days_ago=1)
        _make_vendor_response(db_session, vendor_email="sales@testvendor.com", days_ago=2)

        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["response_rate"] == 1.0

    def test_avg_and_median_response_hours(self, db_session, test_user):
        """Computes correct avg and median response hours."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        now = datetime.now(timezone.utc)

        # Response with 2-hour delay
        _make_vendor_response(
            db_session,
            vendor_email="sales@testvendor.com",
            received_at=now,
            created_at=now - timedelta(hours=2),
        )
        # Response with 6-hour delay
        _make_vendor_response(
            db_session,
            vendor_email="sales@testvendor.com",
            received_at=now - timedelta(days=1),
            created_at=now - timedelta(days=1, hours=6),
        )

        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["avg_response_hours"] == 4.0  # (2 + 6) / 2
        assert result["median_response_hours"] == 4.0  # Even count: (2 + 6) / 2

    def test_median_odd_count(self, db_session, test_user):
        """Median with odd number of responses picks middle value."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        now = datetime.now(timezone.utc)

        for hours in [2, 4, 8]:
            _make_vendor_response(
                db_session,
                vendor_email="sales@testvendor.com",
                received_at=now - timedelta(days=hours),
                created_at=now - timedelta(days=hours, hours=hours),
            )

        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["median_response_hours"] == 4.0  # Middle value

    def test_filters_outlier_response_times(self, db_session, test_user):
        """Filters response times > RESPONSE_MAX_HOURS * 2."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        now = datetime.now(timezone.utc)

        # Normal response
        _make_vendor_response(
            db_session,
            vendor_email="sales@testvendor.com",
            received_at=now,
            created_at=now - timedelta(hours=4),
        )
        # Outlier response (way too old)
        _make_vendor_response(
            db_session,
            vendor_email="sales@testvendor.com",
            received_at=now - timedelta(days=2),
            created_at=now - timedelta(days=60),
        )

        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["avg_response_hours"] == 4.0  # Only the normal one

    def test_quote_quality_rate(self, db_session, test_user):
        """Counts responses with pricing data for quote quality rate."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")

        _make_vendor_response(
            db_session,
            vendor_email="sales@testvendor.com",
            parsed_data={"quotes": [{"price": 0.50}]},
        )
        _make_vendor_response(
            db_session,
            vendor_email="sales@testvendor.com",
            parsed_data=None,
            days_ago=1,
        )

        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["quote_quality_rate"] == 0.5

    def test_matches_by_display_name_when_no_domain(self, db_session, test_user):
        """Falls back to vendor_name matching when vendor has no domain."""
        card = _make_vendor_card(db_session, display_name="No Domain Vendor", domain="")
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        _make_vendor_response(
            db_session,
            vendor_email="random@email.com",
            vendor_name="No Domain Vendor",
        )

        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id)
        assert result["response_count"] == 1

    def test_lookback_days_filter(self, db_session, test_user):
        """Only counts activities within the lookback window."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent", days_ago=200)

        from app.services.response_analytics import compute_vendor_response_metrics

        result = compute_vendor_response_metrics(db_session, card.id, lookback_days=90)
        assert result["outreach_count"] == 0  # Too old


# ── compute_email_health_score() ───────────────────────────────────────


class TestComputeEmailHealthScore:
    """Tests for compute_email_health_score."""

    def test_nonexistent_vendor(self, db_session):
        """Returns empty metrics wrapped in health score structure."""
        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, 99999)
        assert result["metrics"]["response_rate"] == 0.0

    def test_response_time_ideal(self, db_session, test_user):
        """Response time <= 4h scores 100."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        now = datetime.now(timezone.utc)
        _make_vendor_response(
            db_session,
            vendor_email="sales@testvendor.com",
            received_at=now,
            created_at=now - timedelta(hours=2),
        )

        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, card.id)
        assert result["response_time_score"] == 100.0

    def test_response_time_worst(self, db_session, test_user):
        """Response time >= 168h scores 0."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        now = datetime.now(timezone.utc)
        _make_vendor_response(
            db_session,
            vendor_email="sales@testvendor.com",
            received_at=now,
            created_at=now - timedelta(hours=170),
        )

        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, card.id)
        assert result["response_time_score"] == 0.0

    def test_response_time_unknown_defaults_neutral(self, db_session):
        """Unknown response time defaults to 50 (neutral)."""
        card = _make_vendor_card(db_session)

        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, card.id)
        assert result["response_time_score"] == 50.0

    def test_ooo_score_no_contacts(self, db_session):
        """No contacts means OOO score = 100 (no OOO issue)."""
        card = _make_vendor_card(db_session)

        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, card.id)
        assert result["ooo_score"] == 100.0

    def test_ooo_score_with_ooo_contacts(self, db_session):
        """OOO contacts reduce the OOO score."""
        card = _make_vendor_card(db_session)

        # 2 contacts, 1 is OOO
        vc1 = VendorContact(
            vendor_card_id=card.id,
            full_name="A",
            email="a@test.com",
            source="manual",
            is_ooo=False,
        )
        vc2 = VendorContact(
            vendor_card_id=card.id,
            full_name="B",
            email="b@test.com",
            source="manual",
            is_ooo=True,
        )
        db_session.add_all([vc1, vc2])
        db_session.commit()

        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, card.id)
        assert result["ooo_score"] == 50.0  # 1/2 OOO → 50% healthy

    def test_thread_resolution_score_with_domain(self, db_session, test_user):
        """Thread resolution scored from EmailIntelligence thread_summary."""
        card = _make_vendor_card(db_session, domain="vendor.com")

        # Create email intelligence with thread summaries
        ei1 = EmailIntelligence(
            message_id="msg-1",
            user_id=test_user.id,
            sender_email="sales@vendor.com",
            sender_domain="vendor.com",
            classification="offer",
            confidence=0.9,
            thread_summary={"thread_status": "closed"},
        )
        ei2 = EmailIntelligence(
            message_id="msg-2",
            user_id=test_user.id,
            sender_email="sales@vendor.com",
            sender_domain="vendor.com",
            classification="offer",
            confidence=0.9,
            thread_summary={"thread_status": "open"},
        )
        db_session.add_all([ei1, ei2])
        db_session.commit()

        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, card.id)
        assert result["thread_resolution_score"] == 50.0  # 1/2 resolved

    def test_thread_resolution_no_domain(self, db_session):
        """No domain defaults thread resolution to 50 (neutral)."""
        card = _make_vendor_card(db_session, domain="")

        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, card.id)
        assert result["thread_resolution_score"] == 50.0

    def test_composite_score_weighted(self, db_session):
        """Composite score is weighted correctly."""
        card = _make_vendor_card(db_session)

        from app.services.response_analytics import compute_email_health_score

        result = compute_email_health_score(db_session, card.id)
        # With no data: response_rate=0, time=50, quality=0, ooo=100, thread=50
        expected = 0.30 * 0.0 + 0.25 * 50.0 + 0.20 * 0.0 + 0.10 * 100.0 + 0.15 * 50.0
        assert result["email_health_score"] == round(expected, 1)


# ── update_vendor_email_health() ──────────────────────────────────────


class TestUpdateVendorEmailHealth:
    """Tests for update_vendor_email_health."""

    def test_vendor_not_found(self, db_session):
        """Returns None for non-existent vendor."""
        from app.services.response_analytics import update_vendor_email_health

        result = update_vendor_email_health(db_session, 99999)
        assert result is None

    def test_persists_health_score(self, db_session, test_user):
        """Persists computed health score to VendorCard."""
        card = _make_vendor_card(db_session)
        _make_activity_log(db_session, test_user.id, card.id, "rfq_sent")
        now = datetime.now(timezone.utc)
        _make_vendor_response(
            db_session,
            vendor_email="sales@testvendor.com",
            received_at=now,
            created_at=now - timedelta(hours=2),
            parsed_data={"quotes": [{"price": 1.0}]},
        )

        from app.services.response_analytics import update_vendor_email_health

        result = update_vendor_email_health(db_session, card.id)

        assert result is not None
        db_session.refresh(card)
        assert card.email_health_score is not None
        assert card.email_health_computed_at is not None
        assert card.response_rate is not None
        assert card.quote_quality_rate is not None
        assert card.avg_response_hours is not None

    def test_no_avg_response_hours_leaves_field_unchanged(self, db_session):
        """When avg_response_hours is None, the field is not updated."""
        card = _make_vendor_card(db_session, avg_response_hours=5.0)

        from app.services.response_analytics import update_vendor_email_health

        update_vendor_email_health(db_session, card.id)

        db_session.refresh(card)
        # avg_response_hours should remain 5.0 (not overwritten with None)
        assert card.avg_response_hours == 5.0


# ── batch_update_email_health() ──────────────────────────────────────


class TestBatchUpdateEmailHealth:
    """Tests for batch_update_email_health."""

    def test_no_active_vendors(self, db_session):
        """Returns zeros when no vendors have recent activity."""
        from app.services.response_analytics import batch_update_email_health

        result = batch_update_email_health(db_session)
        assert result["updated"] == 0
        assert result["errors"] == 0

    def test_updates_active_vendors(self, db_session):
        """Updates health scores for vendors with recent activity."""
        card = _make_vendor_card(
            db_session,
            last_contact_at=datetime.now(timezone.utc) - timedelta(days=5),
        )

        from app.services.response_analytics import batch_update_email_health

        result = batch_update_email_health(db_session)
        assert result["updated"] == 1

    def test_handles_individual_errors(self, db_session):
        """Counts errors per vendor, doesn't abort batch."""
        _make_vendor_card(
            db_session,
            display_name="Vendor A",
            domain="a.com",
            last_contact_at=datetime.now(timezone.utc) - timedelta(days=5),
        )

        with patch(
            "app.services.response_analytics.update_vendor_email_health",
            side_effect=Exception("scoring failed"),
        ):
            from app.services.response_analytics import batch_update_email_health

            result = batch_update_email_health(db_session)

        assert result["errors"] == 1

    def test_commit_failure_rolls_back(self, db_session):
        """Commit failure is caught and rolled back."""
        _make_vendor_card(
            db_session,
            last_contact_at=datetime.now(timezone.utc) - timedelta(days=5),
        )

        with patch.object(db_session, "commit", side_effect=Exception("commit failed")):
            from app.services.response_analytics import batch_update_email_health

            result = batch_update_email_health(db_session)

        # Updated count is 1, but commit failed
        assert result["updated"] == 1


# ── get_email_intelligence_dashboard() ────────────────────────────────


class TestGetEmailIntelligenceDashboard:
    """Tests for get_email_intelligence_dashboard."""

    def test_empty_dashboard(self, db_session, test_user):
        """Returns zeros for user with no email intelligence data."""
        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert result["emails_scanned_7d"] == 0
        assert result["offers_detected_7d"] == 0
        assert result["stock_lists_7d"] == 0
        assert result["ooo_vendors"] == 0
        assert result["top_vendors"] == []
        assert result["recent_offers"] == []
        assert result["pending_review"] == 0

    def test_counts_scanned_emails(self, db_session, test_user):
        """Counts all EmailIntelligence records within time window."""
        ei = EmailIntelligence(
            message_id="msg-1",
            user_id=test_user.id,
            sender_email="x@y.com",
            sender_domain="y.com",
            classification="general",
            confidence=0.8,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ei)
        db_session.commit()

        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert result["emails_scanned_7d"] == 1

    def test_counts_offers_and_stock_lists(self, db_session, test_user):
        """Counts offer/quote_reply and stock_list classifications separately."""
        now = datetime.now(timezone.utc)
        records = [
            EmailIntelligence(
                message_id="offer-1",
                user_id=test_user.id,
                sender_email="x@y.com",
                sender_domain="y.com",
                classification="offer",
                confidence=0.9,
                created_at=now,
            ),
            EmailIntelligence(
                message_id="qr-1",
                user_id=test_user.id,
                sender_email="x@y.com",
                sender_domain="y.com",
                classification="quote_reply",
                confidence=0.85,
                created_at=now,
            ),
            EmailIntelligence(
                message_id="stock-1",
                user_id=test_user.id,
                sender_email="x@y.com",
                sender_domain="y.com",
                classification="stock_list",
                confidence=0.95,
                created_at=now,
            ),
        ]
        db_session.add_all(records)
        db_session.commit()

        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert result["offers_detected_7d"] == 2  # offer + quote_reply
        assert result["stock_lists_7d"] == 1

    def test_ooo_vendor_count(self, db_session, test_user, test_vendor_card):
        """Counts OOO vendor contacts across all vendors."""
        vc = VendorContact(
            vendor_card_id=test_vendor_card.id,
            full_name="OOO Person",
            email="ooo@test.com",
            source="manual",
            is_ooo=True,
        )
        db_session.add(vc)
        db_session.commit()

        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert result["ooo_vendors"] == 1

    def test_top_vendors(self, db_session, test_user):
        """Returns top vendors sorted by email health score."""
        _make_vendor_card(
            db_session,
            display_name="Top Vendor",
            domain="top.com",
            email_health_score=95.0,
            response_rate=0.9,
        )

        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert len(result["top_vendors"]) == 1
        assert result["top_vendors"][0]["vendor_name"] == "Top Vendor"
        assert result["top_vendors"][0]["email_health_score"] == 95.0

    def test_recent_offers_list(self, db_session, test_user):
        """Returns recent offer EmailIntelligence records."""
        now = datetime.now(timezone.utc)
        ei = EmailIntelligence(
            message_id="recent-offer-1",
            user_id=test_user.id,
            sender_email="vendor@example.com",
            sender_domain="example.com",
            classification="offer",
            confidence=0.92,
            subject="RFQ Reply: LM317T",
            received_at=now,
            auto_applied=True,
            created_at=now,
        )
        db_session.add(ei)
        db_session.commit()

        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert len(result["recent_offers"]) == 1
        assert result["recent_offers"][0]["subject"] == "RFQ Reply: LM317T"
        assert result["recent_offers"][0]["auto_applied"] is True

    def test_pending_review_count(self, db_session, test_user):
        """Counts records needing manual review."""
        ei = EmailIntelligence(
            message_id="review-1",
            user_id=test_user.id,
            sender_email="x@y.com",
            sender_domain="y.com",
            classification="offer",
            confidence=0.6,
            needs_review=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ei)
        db_session.commit()

        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert result["pending_review"] == 1

    def test_avg_response_hours_and_rate(self, db_session, test_user):
        """Returns aggregate avg_response_hours and response_rate."""
        _make_vendor_card(
            db_session,
            display_name="V1",
            domain="v1.com",
            avg_response_hours=4.0,
            response_rate=0.8,
        )
        _make_vendor_card(
            db_session,
            display_name="V2",
            domain="v2.com",
            avg_response_hours=8.0,
            response_rate=0.6,
        )

        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert result["avg_response_hours"] == 6.0  # (4 + 8) / 2
        assert result["response_rate"] == 0.7  # (0.8 + 0.6) / 2

    def test_null_avg_response_returns_none(self, db_session, test_user):
        """Returns None when no vendors have avg_response_hours."""
        from app.services.response_analytics import get_email_intelligence_dashboard

        result = get_email_intelligence_dashboard(db_session, test_user.id)
        assert result["avg_response_hours"] is None
        assert result["response_rate"] is None

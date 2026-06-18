"""tests/test_reporting_service.py — Unit tests for CRM reporting service.

Tests for coverage_report(), pipeline_report(), and outcome_funnel() in
app/services/reporting_service.py.

Called by: pytest
Depends on: app.services.reporting_service, app.models, conftest fixtures
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import ActivityLog, Company, CustomerSite, Quote, Requisition, User

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)


# ─── helpers ────────────────────────────────────────────────────────────────


def _make_company(
    db: Session,
    name: str = "Test Co",
    *,
    tier: str | None = None,
    last_outbound_at: datetime | None = None,
    owner_id: int | None = None,
) -> Company:
    co = Company(
        name=name,
        is_active=True,
        tier=tier,
        last_outbound_at=last_outbound_at,
        account_owner_id=owner_id,
    )
    db.add(co)
    db.flush()
    return co


def _make_site(db: Session, company: Company) -> CustomerSite:
    site = CustomerSite(company_id=company.id, site_name="HQ")
    db.add(site)
    db.flush()
    return site


def _make_req(
    db: Session,
    status: str,
    *,
    value: float | None = None,
    created_at: datetime | None = None,
) -> Requisition:
    req = Requisition(
        name=f"REQ-{status[:4].upper()}-001",
        customer_name="Test Co",
        status=status,
        opportunity_value=value,
        created_at=created_at or NOW,
    )
    db.add(req)
    db.flush()
    return req


def _make_quote(
    db: Session,
    *,
    sent_at: datetime | None = None,
    result: str | None = None,
    result_at: datetime | None = None,
    subtotal: float = 1000.0,
) -> Quote:
    req = _make_req(db, "won" if result == "won" else "quoted")
    q = Quote(
        requisition_id=req.id,
        quote_number=f"Q-{req.id}-001",
        status="sent",
        line_items=[],
        subtotal=subtotal,
        total_cost=subtotal * 0.5,
        total_margin_pct=50.0,
        sent_at=sent_at,
        result=result,
        result_at=result_at,
    )
    db.add(q)
    db.flush()
    return q


def _make_activity(
    db: Session,
    activity_type: str,
    *,
    company_id: int | None = None,
    created_at: datetime | None = None,
) -> ActivityLog:
    log = ActivityLog(
        activity_type=activity_type,
        channel="email",
        company_id=company_id,
        created_at=created_at or NOW,
    )
    db.add(log)
    db.flush()
    return log


# ─── coverage_report ────────────────────────────────────────────────────────


class TestCoverageReport:
    def test_empty_db(self, db_session: Session):
        """Returns zero summary when no companies exist."""
        from app.services.reporting_service import coverage_report

        result = coverage_report(db_session)

        assert result["summary"]["total"] == 0
        assert result["summary"]["overdue"] == 0
        assert result["summary"]["overdue_pct"] == 0.0
        # by_tier may contain zero-count placeholder rows for each tier, but
        # every row must have zero total
        for row in result["by_tier"]:
            assert row["total"] == 0
        assert result["by_rep"] == []

    def test_on_target_company(self, db_session: Session):
        """A key-tier company touched 3 days ago is on_target."""
        from app.services.reporting_service import coverage_report

        _make_company(
            db_session,
            "Key Co",
            tier="key",
            last_outbound_at=NOW - timedelta(days=3),
        )
        db_session.commit()

        result = coverage_report(db_session)
        assert result["summary"]["total"] == 1
        assert result["summary"]["overdue"] == 0
        tier_row = next((r for r in result["by_tier"] if r["tier"] == "key"), None)
        assert tier_row is not None
        assert tier_row["on_target"] == 1
        assert tier_row["overdue"] == 0
        assert tier_row["coverage_pct"] == 100.0

    def test_overdue_company_60_days(self, db_session: Session):
        """A company with last_outbound_at = 60 days ago falls in overdue bucket."""
        from app.services.reporting_service import coverage_report

        _make_company(
            db_session,
            "Stale Co",
            tier="standard",
            last_outbound_at=NOW - timedelta(days=60),
        )
        db_session.commit()

        result = coverage_report(db_session)
        assert result["summary"]["overdue"] == 1
        tier_row = next(r for r in result["by_tier"] if r["tier"] == "standard")
        assert tier_row["overdue"] == 1
        assert tier_row["coverage_pct"] == 0.0

    def test_new_company_no_outbound(self, db_session: Session):
        """A company never contacted has state 'new'."""
        from app.services.reporting_service import coverage_report

        _make_company(db_session, "New Co", tier="prospect", last_outbound_at=None)
        db_session.commit()

        result = coverage_report(db_session)
        tier_row = next(r for r in result["by_tier"] if r["tier"] == "prospect")
        assert tier_row["new"] == 1
        assert tier_row["overdue"] == 0

    def test_by_rep_grouping(self, db_session: Session):
        """Companies are grouped by account owner name."""
        from app.services.reporting_service import coverage_report

        owner = User(
            email="rep@test.com",
            name="Alice Rep",
            role="sales",
            azure_id="az-rep-001",
        )
        db_session.add(owner)
        db_session.flush()

        _make_company(db_session, "A", tier="core", last_outbound_at=NOW - timedelta(days=5), owner_id=owner.id)
        _make_company(db_session, "B", tier="core", last_outbound_at=NOW - timedelta(days=60), owner_id=owner.id)
        db_session.commit()

        result = coverage_report(db_session)
        rep_row = next(r for r in result["by_rep"] if r["rep"] == "Alice Rep")
        assert rep_row["total"] == 2
        assert rep_row["overdue"] == 1
        assert rep_row["coverage_pct"] == 50.0

    def test_overdue_pct_calculation(self, db_session: Session):
        """overdue_pct = overdue / total * 100."""
        from app.services.reporting_service import coverage_report

        _make_company(db_session, "A", tier="standard", last_outbound_at=NOW - timedelta(days=60))
        _make_company(db_session, "B", tier="standard", last_outbound_at=NOW - timedelta(days=60))
        _make_company(db_session, "C", tier="standard", last_outbound_at=NOW - timedelta(days=5))
        _make_company(db_session, "D", tier="standard", last_outbound_at=NOW - timedelta(days=5))
        db_session.commit()

        result = coverage_report(db_session)
        assert result["summary"]["total"] == 4
        assert result["summary"]["overdue"] == 2
        assert result["summary"]["overdue_pct"] == 50.0


# ─── pipeline_report ────────────────────────────────────────────────────────


class TestPipelineReport:
    def test_empty_db(self, db_session: Session):
        """Returns zero stages and metrics when no requisitions exist."""
        from app.services.reporting_service import pipeline_report

        result = pipeline_report(db_session)

        assert result["win_rate"] == 0.0
        assert result["total_open_value"] == 0.0
        assert result["avg_deal_value"] == 0.0
        for stage in result["stages"]:
            assert stage["count"] == 0
            assert stage["value"] == 0.0

    def test_stage_grouping(self, db_session: Session):
        """Requisitions are grouped into correct stage buckets."""
        from app.services.reporting_service import pipeline_report

        _make_req(db_session, "draft")
        _make_req(db_session, "active")
        _make_req(db_session, "sourcing")
        _make_req(db_session, "quoting")
        _make_req(db_session, "won")
        _make_req(db_session, "lost")
        # excluded statuses — must NOT appear in any stage
        _make_req(db_session, "archived")
        _make_req(db_session, "cancelled")
        db_session.commit()

        result = pipeline_report(db_session)
        stage_map = {s["name"]: s for s in result["stages"]}

        assert stage_map["Active"]["count"] == 2
        assert stage_map["Sourcing"]["count"] == 1
        assert stage_map["Quoting"]["count"] == 1
        assert stage_map["Won"]["count"] == 1
        assert stage_map["Lost"]["count"] == 1
        # Total across all stages = 6 (archived + cancelled excluded)
        total = sum(s["count"] for s in result["stages"])
        assert total == 6

    def test_win_rate_two_won_one_lost(self, db_session: Session):
        """Win rate = won / (won + lost) * 100, rounded to 1dp."""
        from app.services.reporting_service import pipeline_report

        _make_req(db_session, "won")
        _make_req(db_session, "won")
        _make_req(db_session, "lost")
        db_session.commit()

        result = pipeline_report(db_session)
        assert result["win_rate"] == pytest.approx(66.7, abs=0.2)

    def test_avg_deal_value(self, db_session: Session):
        """avg_deal_value = won opportunity_value / won count."""
        from app.services.reporting_service import pipeline_report

        _make_req(db_session, "won", value=3000.0)
        _make_req(db_session, "won", value=1000.0)
        db_session.commit()

        result = pipeline_report(db_session)
        assert result["avg_deal_value"] == pytest.approx(2000.0, abs=0.01)

    def test_period_label_all_time(self, db_session: Session):
        """period_label is 'All time' when days is None."""
        from app.services.reporting_service import pipeline_report

        result = pipeline_report(db_session)
        assert result["period_label"] == "All time"

    def test_period_label_with_days(self, db_session: Session):
        """period_label includes the days value when days is specified."""
        from app.services.reporting_service import pipeline_report

        result = pipeline_report(db_session, days=30)
        assert "30" in result["period_label"]

    def test_no_win_rate_when_no_decided(self, db_session: Session):
        """Win rate is 0.0 when there are no won or lost requisitions."""
        from app.services.reporting_service import pipeline_report

        _make_req(db_session, "active")
        _make_req(db_session, "quoting")
        db_session.commit()

        result = pipeline_report(db_session)
        assert result["win_rate"] == 0.0

    def test_total_open_value_excludes_won_lost(self, db_session: Session):
        """total_open_value sums Active+Sourcing+Quoting but not Won or Lost."""
        from app.services.reporting_service import pipeline_report

        _make_req(db_session, "active", value=500.0)
        _make_req(db_session, "sourcing", value=300.0)
        _make_req(db_session, "won", value=9999.0)
        _make_req(db_session, "lost", value=9999.0)
        db_session.commit()

        result = pipeline_report(db_session)
        assert result["total_open_value"] == pytest.approx(800.0, abs=0.01)


# ─── outcome_funnel ─────────────────────────────────────────────────────────


class TestOutcomeFunnel:
    def test_empty_db(self, db_session: Session):
        """Returns zero counts and conversions when db is empty."""
        from app.services.reporting_service import outcome_funnel

        result = outcome_funnel(db_session, days=90)

        assert result["days"] == 90
        for step in result["steps"]:
            assert step["count"] == 0
        assert result["conv_rfq"] == 0.0
        assert result["conv_quote"] == 0.0
        assert result["conv_won"] == 0.0

    def test_interaction_types_counted(self, db_session: Session):
        """Counts email_sent, email_received, call_logged, teams_message as
        interactions."""
        from app.services.reporting_service import outcome_funnel

        co = _make_company(db_session, "Funnel Co")
        for atype in ("email_sent", "email_received", "call_logged", "teams_message"):
            _make_activity(db_session, atype, company_id=co.id, created_at=NOW - timedelta(days=10))
        # activity without company_id must NOT be counted
        _make_activity(db_session, "email_sent", company_id=None, created_at=NOW - timedelta(days=10))
        db_session.commit()

        result = outcome_funnel(db_session, days=90)
        interactions_step = next(s for s in result["steps"] if s["label"] == "Interactions")
        assert interactions_step["count"] == 4

    def test_rfq_counted(self, db_session: Session):
        """rfq_sent activity type is counted as an RFQ step."""
        from app.services.reporting_service import outcome_funnel

        _make_activity(db_session, "rfq_sent", created_at=NOW - timedelta(days=5))
        _make_activity(db_session, "rfq_sent", created_at=NOW - timedelta(days=5))
        db_session.commit()

        result = outcome_funnel(db_session, days=90)
        rfq_step = next(s for s in result["steps"] if s["label"] == "RFQs Sent")
        assert rfq_step["count"] == 2

    def test_quotes_sent_and_won(self, db_session: Session):
        """Quote.sent_at and result='won' are counted in their funnel steps."""
        from app.services.reporting_service import outcome_funnel

        _make_quote(db_session, sent_at=NOW - timedelta(days=10))
        _make_quote(db_session, sent_at=NOW - timedelta(days=20), result="won", result_at=NOW - timedelta(days=5))
        db_session.commit()

        result = outcome_funnel(db_session, days=90)
        quotes_step = next(s for s in result["steps"] if s["label"] == "Quotes Sent")
        won_step = next(s for s in result["steps"] if s["label"] == "Won")
        assert quotes_step["count"] == 2
        assert won_step["count"] == 1

    def test_cutoff_excludes_old_entries(self, db_session: Session):
        """Activities and quotes older than the window are excluded."""
        from app.services.reporting_service import outcome_funnel

        co = _make_company(db_session, "Old Co")
        # These are 100 days old — outside the 90-day window
        _make_activity(db_session, "email_sent", company_id=co.id, created_at=NOW - timedelta(days=100))
        _make_quote(db_session, sent_at=NOW - timedelta(days=100))
        db_session.commit()

        result = outcome_funnel(db_session, days=90)
        for step in result["steps"]:
            assert step["count"] == 0

    def test_conv_rfq_rate(self, db_session: Session):
        """conv_rfq = rfqs / interactions * 100."""
        from app.services.reporting_service import outcome_funnel

        co = _make_company(db_session, "Conv Co")
        for _ in range(4):
            _make_activity(db_session, "email_sent", company_id=co.id, created_at=NOW - timedelta(days=10))
        for _ in range(2):
            _make_activity(db_session, "rfq_sent", created_at=NOW - timedelta(days=10))
        db_session.commit()

        result = outcome_funnel(db_session, days=90)
        assert result["conv_rfq"] == pytest.approx(50.0, abs=0.1)

"""tests/test_reporting_service.py — Unit tests for CRM cadence-coverage service.

Tests for coverage_report() in app/services/reporting_service.py. Pipeline/forecast
and the conversion funnel moved to forecast_service (see tests/test_forecast_service.py).

Called by: pytest
Depends on: app.services.reporting_service, app.models, conftest fixtures
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Company, User

NOW = datetime.now(UTC)


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

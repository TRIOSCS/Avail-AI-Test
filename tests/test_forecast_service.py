"""Tests for CRM Phase 5b — forecast_service pipeline/forecast rollups.

Called by: pytest
Depends on: app.services.forecast_service, app.models (Requisition, Requirement,
            Quote, Company, User)
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, Quote, Requirement, Requisition, User
from app.services.forecast_service import (
    OPEN_STATUSES,
    STAGE_WIN_PROBABILITY,
    bulk_deal_values,
    conversion_funnel,
    pipeline_by_account,
    pipeline_by_owner,
    pipeline_summary,
    stage_probability,
)
from tests.conftest import engine  # noqa: F401


def _req(db, created_by, *, status="active", value=None, company_id=None, claimed_by_id=None, created_at=None):
    req = Requisition(
        name=f"Req {status}",
        status=status,
        created_by=created_by,
        opportunity_value=value,
        company_id=company_id,
        claimed_by_id=claimed_by_id,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


class TestStageProbability:
    def test_known_statuses(self):
        assert stage_probability("sourcing") == 0.25
        assert stage_probability("quoted") == 0.75
        assert stage_probability("won") == 1.0
        assert stage_probability("lost") == 0.0

    def test_unknown_and_none(self):
        assert stage_probability("nonsense") == 0.0
        assert stage_probability(None) == 0.0

    def test_open_statuses_are_strictly_live(self):
        # Open = 0 < p < 1 — excludes won (1.0) and dead (0.0).
        assert "won" not in OPEN_STATUSES
        assert "lost" not in OPEN_STATUSES
        assert "sourcing" in OPEN_STATUSES
        assert OPEN_STATUSES == frozenset(s for s, p in STAGE_WIN_PROBABILITY.items() if 0.0 < p < 1.0)


class TestBulkDealValues:
    def test_uses_opportunity_value_when_entered(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user.id, value=100000)
        vals = bulk_deal_values(db_session, [req.id])
        assert vals[req.id] == 100000.0

    def test_computed_from_requirements_when_no_opp_value(self, db_session: Session, test_user: User):
        req = _req(db_session, test_user.id, value=None)
        db_session.add(Requirement(requisition_id=req.id, target_price=10, target_qty=5))
        db_session.add(Requirement(requisition_id=req.id, target_price=20, target_qty=2))
        db_session.flush()
        vals = bulk_deal_values(db_session, [req.id])
        # 10*5 + 20*2 = 90
        assert vals[req.id] == 90.0

    def test_empty_ids(self, db_session: Session):
        assert bulk_deal_values(db_session, []) == {}


class TestPipelineSummary:
    def test_weighted_math(self, db_session: Session, test_user: User):
        _req(db_session, test_user.id, status="sourcing", value=100000)
        summary = pipeline_summary(db_session)
        assert summary["open_count"] == 1
        assert summary["open_value"] == 100000.0
        # 100000 * 0.25 = 25000
        assert summary["weighted_value"] == 25000.0

    def test_won_lost_win_rate(self, db_session: Session, test_user: User):
        _req(db_session, test_user.id, status="won", value=50000)
        _req(db_session, test_user.id, status="won", value=30000)
        _req(db_session, test_user.id, status="lost", value=10000)
        summary = pipeline_summary(db_session)
        assert summary["won_count"] == 2
        assert summary["lost_count"] == 1
        assert summary["won_value"] == 80000.0
        assert summary["win_rate"] == pytest.approx(2 / 3)

    def test_win_rate_zero_when_nothing_decided(self, db_session: Session, test_user: User):
        _req(db_session, test_user.id, status="sourcing", value=1000)
        summary = pipeline_summary(db_session)
        assert summary["win_rate"] == 0.0

    def test_by_stage_buckets(self, db_session: Session, test_user: User):
        _req(db_session, test_user.id, status="sourcing", value=100000)
        _req(db_session, test_user.id, status="quoted", value=40000)
        summary = pipeline_summary(db_session)
        by_stage = {b["status"]: b for b in summary["by_stage"]}
        assert by_stage["sourcing"]["count"] == 1
        assert by_stage["sourcing"]["weighted"] == 25000.0
        assert by_stage["quoted"]["count"] == 1
        assert by_stage["quoted"]["weighted"] == 30000.0  # 40000 * 0.75
        # Open stages only; won/lost never appear here.
        assert "won" not in by_stage

    def test_owner_scoping(self, db_session: Session, test_user: User):
        other = User(name="Other Rep", email="other@example.com", role="sales")
        db_session.add(other)
        db_session.flush()
        _req(db_session, test_user.id, status="sourcing", value=100000, claimed_by_id=test_user.id)
        _req(db_session, test_user.id, status="sourcing", value=200000, claimed_by_id=other.id)
        mine = pipeline_summary(db_session, owner_id=test_user.id)
        assert mine["open_value"] == 100000.0


class TestPipelineByAccount:
    def test_groups_and_ranks_by_weighted(self, db_session: Session, test_user: User):
        acme = Company(name="Acme Corp", is_active=True)
        globex = Company(name="Globex", is_active=True)
        db_session.add_all([acme, globex])
        db_session.flush()
        _req(db_session, test_user.id, status="quoted", value=100000, company_id=acme.id)  # w=75000
        _req(db_session, test_user.id, status="sourcing", value=100000, company_id=globex.id)  # w=25000
        rows = pipeline_by_account(db_session)
        assert [r["company_name"] for r in rows] == ["Acme Corp", "Globex"]
        assert rows[0]["weighted_value"] == 75000.0

    def test_skips_reqs_without_company(self, db_session: Session, test_user: User):
        _req(db_session, test_user.id, status="sourcing", value=100000, company_id=None)
        assert pipeline_by_account(db_session) == []


class TestPipelineByOwner:
    def test_unassigned_bucket(self, db_session: Session, test_user: User):
        _req(db_session, test_user.id, status="sourcing", value=100000, claimed_by_id=None)
        rows = pipeline_by_owner(db_session)
        assert len(rows) == 1
        assert rows[0]["owner_id"] is None
        assert rows[0]["owner_name"] == "Unassigned"
        assert rows[0]["weighted_value"] == 25000.0

    def test_named_owner_and_won_value(self, db_session: Session, test_user: User):
        _req(db_session, test_user.id, status="won", value=50000, claimed_by_id=test_user.id)
        rows = pipeline_by_owner(db_session)
        owner = next(r for r in rows if r["owner_id"] == test_user.id)
        assert owner["owner_name"] == (test_user.name or test_user.email)
        assert owner["won_value"] == 50000.0


class TestConversionFunnel:
    def test_status_progression(self, db_session: Session, test_user: User):
        _req(db_session, test_user.id, status="draft")
        _req(db_session, test_user.id, status="sourcing")
        won = _req(db_session, test_user.id, status="won")
        db_session.add(Quote(requisition_id=won.id, quote_number="Q-FUNNEL-1", status="won"))
        db_session.flush()
        funnel = conversion_funnel(db_session)
        assert funnel["opportunities"] == 3
        assert funnel["sourcing"] == 2  # sourcing + won (not draft)
        assert funnel["quoted"] == 1  # the won req (status won) / has a quote
        assert funnel["won"] == 1
        assert funnel["window_days"] == 90

    def test_excludes_old_requisitions(self, db_session: Session, test_user: User):
        old = datetime.now(timezone.utc) - timedelta(days=200)
        _req(db_session, test_user.id, status="sourcing", created_at=old)
        funnel = conversion_funnel(db_session, days=90)
        assert funnel["opportunities"] == 0

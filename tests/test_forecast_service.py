"""Tests for CRM Phase 5b — forecast_service pipeline/forecast rollups.

Called by: pytest
Depends on: app.services.forecast_service, app.models (Requisition, Requirement,
            Quote, Company, User)
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User
from app.services.forecast_service import (
    OPEN_STATUSES,
    STAGE_WIN_PROBABILITY,
    bulk_deal_values,
    pipeline_summary,
    stage_probability,
)
from tests.conftest import engine  # noqa: F401


def _req(db, created_by, *, status="open", value=None, company_id=None, claimed_by_id=None, created_at=None):
    req = Requisition(
        name=f"Req {status}",
        status=status,
        created_by=created_by,
        opportunity_value=value,
        company_id=company_id,
        claimed_by_id=claimed_by_id,
        created_at=created_at or datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    return req


class TestStageProbability:
    def test_known_statuses(self):
        assert stage_probability("open") == 0.10
        assert stage_probability("rfqs_sent") == 0.25
        assert stage_probability("offers") == 0.40
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
        assert "rfqs_sent" in OPEN_STATUSES
        assert OPEN_STATUSES == frozenset(s for s, p in STAGE_WIN_PROBABILITY.items() if 0.0 < p < 1.0)

    def test_live_pipeline_statuses_are_forecastable(self):
        # Regression guard: the LIVE open statuses written by real requisitions
        # (migration 158) must all carry a non-zero win probability AND be in
        # OPEN_STATUSES — otherwise pipeline_summary silently zeros out (the
        # exact bug the old draft/active/sourcing vocabulary caused).
        for status in ("open", "rfqs_sent", "offers", "quoted"):
            assert status in STAGE_WIN_PROBABILITY, f"{status} missing from ladder"
            assert STAGE_WIN_PROBABILITY[status] > 0.0, f"{status} has zero probability"
            assert status in OPEN_STATUSES, f"{status} not counted as open"

    def test_terminal_and_monitor_excluded_from_open(self):
        # hotlist is a MONITOR state (no win probability); won/lost/cancelled are
        # TERMINAL. None of them belong to the open pipeline.
        for status in ("hotlist", "won", "lost", "cancelled"):
            assert status not in OPEN_STATUSES, f"{status} must not be open"
        # hotlist carries no win probability at all (off-pipeline monitor).
        assert "hotlist" not in STAGE_WIN_PROBABILITY
        assert stage_probability("hotlist") == 0.0


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
        _req(db_session, test_user.id, status="rfqs_sent", value=100000)
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
        _req(db_session, test_user.id, status="rfqs_sent", value=1000)
        summary = pipeline_summary(db_session)
        assert summary["win_rate"] == 0.0

    def test_by_stage_buckets(self, db_session: Session, test_user: User):
        _req(db_session, test_user.id, status="rfqs_sent", value=100000)
        _req(db_session, test_user.id, status="quoted", value=40000)
        summary = pipeline_summary(db_session)
        by_stage = {b["status"]: b for b in summary["by_stage"]}
        assert by_stage["rfqs_sent"]["count"] == 1
        assert by_stage["rfqs_sent"]["weighted"] == 25000.0
        assert by_stage["quoted"]["count"] == 1
        assert by_stage["quoted"]["weighted"] == 30000.0  # 40000 * 0.75
        # Open stages only; won/lost never appear here.
        assert "won" not in by_stage

    def test_owner_scoping(self, db_session: Session, test_user: User):
        other = User(name="Other Rep", email="other@example.com", role="sales")
        db_session.add(other)
        db_session.flush()
        _req(db_session, test_user.id, status="rfqs_sent", value=100000, claimed_by_id=test_user.id)
        _req(db_session, test_user.id, status="rfqs_sent", value=200000, claimed_by_id=other.id)
        mine = pipeline_summary(db_session, owner_id=test_user.id)
        assert mine["open_value"] == 100000.0

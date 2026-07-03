"""test_quality_plan_service.py — TDD tests for QualityPlanService.

Tests:
  1. create_qp returns a QP in DRAFT with created_by_id set.
  2. validate_complete flags a QP missing buy_plan_id (pure in-memory check).
  3. validate_complete passes when all required fields are set.

Called by: pytest (TESTING=1 PYTHONPATH=. python -m pytest tests/test_quality_plan_service.py -q)
Depends on: app.services.quality_plan_service, conftest (db_session, test_user, test_company)

Note: quality_plans.buy_plan_id is NOT NULL in the schema, so create_qp always
requires a buy_plan_id. validate_complete is tested as a pure function by
constructing an ORM object in memory (without flushing) to simulate the missing field.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.constants import QPOrderType, QualityPlanStatus
from app.models.buy_plan import BuyPlan
from app.models.quality_plan import QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.services.quality_plan_service import create_qp, validate_complete

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_requisition(db: Session, company_id: int, owner_id: int) -> Requisition:
    """Minimal Requisition row for BuyPlan FK."""
    req = Requisition(
        name="TEST-001",
        status="open",
        company_id=company_id,
        created_by=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_quote(db: Session, requisition_id: int) -> Quote:
    """Minimal Quote row for BuyPlan FK (quote_number is NOT NULL)."""
    q = Quote(
        requisition_id=requisition_id,
        quote_number="QT-TEST-001",
        status="draft",
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    return q


def _make_buy_plan(db: Session, requisition_id: int, quote_id: int) -> BuyPlan:
    """Minimal BuyPlan row."""
    bp = BuyPlan(
        requisition_id=requisition_id,
        quote_id=quote_id,
        status="draft",
        created_at=datetime.now(timezone.utc),
    )
    db.add(bp)
    db.flush()
    return bp


def _make_full_buy_plan(db: Session, test_user, test_company) -> BuyPlan:
    """Helper: create Requisition → Quote → BuyPlan and return the BuyPlan."""
    req = _make_requisition(db, test_company.id, test_user.id)
    q = _make_quote(db, req.id)
    return _make_buy_plan(db, req.id, q.id)


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_create_qp_returns_draft(db_session: Session, test_user, test_company):
    """create_qp returns a QualityPlan in DRAFT status with created_by_id set."""
    bp = _make_full_buy_plan(db_session, test_user, test_company)
    qp = create_qp(db_session, owner_id=test_user.id, buy_plan_id=bp.id)
    assert qp.id is not None
    assert qp.status == QualityPlanStatus.DRAFT
    assert qp.created_by_id == test_user.id
    assert qp.buy_plan_id == bp.id


def test_validate_complete_flags_missing_buy_plan(test_user):
    """validate_complete returns a non-empty error list when buy_plan_id is absent.

    Uses an in-memory QP object (not flushed) to avoid the NOT NULL DB constraint.
    """
    # Construct the ORM object directly without touching the DB
    qp = QualityPlan(
        created_by_id=test_user.id,
        order_type=QPOrderType.NEW,
        status=QualityPlanStatus.DRAFT,
        buy_plan_id=None,  # deliberately missing
    )
    errors = validate_complete(qp)
    assert any("buy_plan" in e.lower() for e in errors), f"Expected buy_plan error, got: {errors}"


def test_validate_complete_passes_when_complete(db_session: Session, test_user, test_company):
    """validate_complete returns [] when all required fields are present."""
    bp = _make_full_buy_plan(db_session, test_user, test_company)
    qp = create_qp(db_session, owner_id=test_user.id, buy_plan_id=bp.id)
    qp.order_type = QPOrderType.NEW
    errors = validate_complete(qp)
    assert errors == []

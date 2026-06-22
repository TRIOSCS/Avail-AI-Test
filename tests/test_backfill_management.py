"""Tests for app/management/backfill_buyplan_cph.py and backfill_quote_source.py."""

import os
import uuid

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus


def _make_requisition(db: Session):
    from app.models.crm import Company
    from app.models.sourcing import Requisition

    co = Company(name="Test Co", is_active=True, created_at=datetime.now(timezone.utc))
    db.add(co)
    db.flush()
    req = Requisition(
        company_id=co.id,
        name="Test Requisition",
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_buy_plan(db: Session, status: str, requisition_id: int, recorded: bool = False):
    from app.models.buy_plan import BuyPlan
    from app.models.quotes import Quote

    # BuyPlan requires a quote_id; Quote requires a requisition_id and quote_number
    q = Quote(
        requisition_id=requisition_id,
        quote_number=f"QT-{uuid.uuid4().hex[:8].upper()}",
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()

    plan = BuyPlan(
        quote_id=q.id,
        requisition_id=requisition_id,
        status=status,
        purchase_history_recorded_at=datetime.now(timezone.utc) if recorded else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(plan)
    db.flush()
    return plan


# ── backfill_buyplan_cph ──────────────────────────────────────────────────────


def test_backfill_buyplan_cph_records_completed_plans(db_session: Session):
    """Completed plans without history are processed; returns count."""
    from unittest.mock import patch

    from app.management.backfill_buyplan_cph import backfill

    req = _make_requisition(db_session)
    plan = _make_buy_plan(db_session, BuyPlanStatus.COMPLETED.value, req.id)
    db_session.commit()

    with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_record:
        count = backfill(db_session)

    assert count >= 1
    mock_record.assert_called()


def test_backfill_buyplan_cph_skips_already_recorded(db_session: Session):
    """Plans already recorded (purchase_history_recorded_at set) are skipped."""
    from unittest.mock import patch

    from app.management.backfill_buyplan_cph import backfill

    req = _make_requisition(db_session)
    plan = _make_buy_plan(db_session, BuyPlanStatus.COMPLETED.value, req.id, recorded=True)
    db_session.commit()

    with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_record:
        count = backfill(db_session)

    assert count == 0
    mock_record.assert_not_called()


def test_backfill_buyplan_cph_skips_non_completed(db_session: Session):
    """Only COMPLETED plans are processed; drafts are ignored."""
    from unittest.mock import patch

    from app.management.backfill_buyplan_cph import backfill

    req = _make_requisition(db_session)
    _make_buy_plan(db_session, BuyPlanStatus.DRAFT.value, req.id)
    db_session.commit()

    with patch("app.management.backfill_buyplan_cph.record_buyplan_purchase_history") as mock_record:
        count = backfill(db_session)

    assert count == 0
    mock_record.assert_not_called()


def test_backfill_buyplan_cph_empty_db(db_session: Session):
    """Returns 0 when no plans exist."""
    from app.management.backfill_buyplan_cph import backfill

    count = backfill(db_session)
    assert count == 0


# ── backfill_quote_source ─────────────────────────────────────────────────────


def _make_quote(db: Session, source=None, requisition_id: int | None = None):
    from app.models.crm import Company
    from app.models.quotes import Quote
    from app.models.sourcing import Requisition

    if requisition_id is None:
        co = Company(name="Quote Co", is_active=True, created_at=datetime.now(timezone.utc))
        db.add(co)
        db.flush()
        req = Requisition(
            company_id=co.id,
            name="Quote Req",
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db.add(req)
        db.flush()
        requisition_id = req.id

    q = Quote(
        requisition_id=requisition_id,
        quote_number=f"QT-{uuid.uuid4().hex[:8].upper()}",
        source=source,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    return q


def _make_proactive_offer(db: Session, quote_id: int):
    from app.models.intelligence import ProactiveOffer

    po = ProactiveOffer(
        converted_quote_id=quote_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(po)
    db.flush()
    return po


def test_backfill_quote_source_sets_proactive(db_session: Session):
    """Quotes linked via ProactiveOffer.converted_quote_id get source='proactive'."""
    from app.management.backfill_quote_source import backfill

    quote = _make_quote(db_session, source=None)
    _make_proactive_offer(db_session, quote.id)
    db_session.commit()

    count = backfill(db_session)

    assert count >= 1
    db_session.refresh(quote)
    assert quote.source == "proactive"


def test_backfill_quote_source_skips_already_set(db_session: Session):
    """Quotes that already have a source are not modified."""
    from app.management.backfill_quote_source import backfill

    quote = _make_quote(db_session, source="manual")
    _make_proactive_offer(db_session, quote.id)
    db_session.commit()

    count = backfill(db_session)

    assert count == 0
    db_session.refresh(quote)
    assert quote.source == "manual"


def test_backfill_quote_source_empty_db(db_session: Session):
    """Returns 0 when no proactive offers exist."""
    from app.management.backfill_quote_source import backfill

    count = backfill(db_session)
    assert count == 0


def test_backfill_quote_source_no_null_source_quotes(db_session: Session):
    """Proactive offers pointing to quotes with a source set are not updated."""
    from app.management.backfill_quote_source import backfill

    q1 = _make_quote(db_session, source="email")
    q2 = _make_quote(db_session, source=None)
    _make_proactive_offer(db_session, q1.id)
    _make_proactive_offer(db_session, q2.id)
    db_session.commit()

    count = backfill(db_session)

    assert count == 1
    db_session.refresh(q2)
    assert q2.source == "proactive"
    db_session.refresh(q1)
    assert q1.source == "email"


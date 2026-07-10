from datetime import UTC, datetime, timedelta

from app.constants import ActivityType, Channel, Direction
from app.models.crm import Company
from app.models.intelligence import ActivityLog
from app.services.cadence_service import bump_clocks_from_activity

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)


def _mk(db, co, *, direction, meaningful, created):
    a = ActivityLog(
        activity_type=ActivityType.EMAIL_RECEIVED,
        channel=Channel.EMAIL,
        company_id=co.id,
        direction=direction,
        is_meaningful=meaningful,
        created_at=created,
        occurred_at=created,
    )
    db.add(a)
    db.flush()
    return a


def test_outbound_sets_outbound_clock(db_session):
    co = Company(name="Bump Co")
    db_session.add(co)
    db_session.flush()
    bump_clocks_from_activity(
        db_session, _mk(db_session, co, direction=Direction.OUTBOUND, meaningful=None, created=NOW)
    )
    db_session.refresh(co)
    assert co.last_outbound_at == NOW and co.last_reply_at is None


def test_meaningful_inbound_sets_reply_clock(db_session):
    co = Company(name="Bump Co2")
    db_session.add(co)
    db_session.flush()
    bump_clocks_from_activity(
        db_session, _mk(db_session, co, direction=Direction.INBOUND, meaningful=True, created=NOW)
    )
    db_session.refresh(co)
    assert co.last_reply_at == NOW and co.last_outbound_at is None


def test_noise_inbound_does_not_set_reply_clock(db_session):
    co = Company(name="Bump Co3")
    db_session.add(co)
    db_session.flush()
    bump_clocks_from_activity(
        db_session, _mk(db_session, co, direction=Direction.INBOUND, meaningful=False, created=NOW)
    )
    db_session.refresh(co)
    assert co.last_reply_at is None


def test_clock_only_advances_forward(db_session):
    co = Company(name="Bump Co4", last_outbound_at=NOW)
    db_session.add(co)
    db_session.flush()
    # older outbound must NOT move the clock backward
    bump_clocks_from_activity(
        db_session, _mk(db_session, co, direction=Direction.OUTBOUND, meaningful=None, created=NOW - timedelta(days=3))
    )
    db_session.refresh(co)
    assert co.last_outbound_at == NOW


from app.services.activity_service import log_company_call


def test_log_company_call_advances_outbound_clock(db_session):
    co = Company(name="Call Co")
    db_session.add(co)
    db_session.flush()
    log_company_call(
        user_id=None,
        company_id=co.id,
        direction="outbound",
        phone="+15551234567",
        duration_seconds=120,
        contact_name="Buyer",
        notes="left details",
        db=db_session,
    )
    db_session.refresh(co)
    assert co.last_outbound_at is not None

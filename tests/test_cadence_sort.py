from datetime import UTC, datetime, timedelta

from app.models.crm import Company
from app.services.crm_service import order_by_clock

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)


def test_null_clock_sorts_first_then_oldest(db_session):
    recent = Company(name="Recent", last_outbound_at=NOW - timedelta(days=1))
    old = Company(name="Old", last_outbound_at=NOW - timedelta(days=20))
    never = Company(name="Never")  # NULL clock
    db_session.add_all([recent, old, never])
    db_session.commit()

    rows = order_by_clock(db_session.query(Company), "outbound").all()
    assert [c.name for c in rows] == ["Never", "Old", "Recent"]

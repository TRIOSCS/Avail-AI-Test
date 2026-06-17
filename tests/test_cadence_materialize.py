from datetime import datetime, timedelta, timezone

from app.constants import ActivityType, Channel, Direction
from app.models.crm import Company, CustomerSite, SiteContact
from app.models.intelligence import ActivityLog
from app.services.cadence_service import materialize_company_clocks

NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def _log(db, *, company_id, site_contact_id=None, customer_site_id=None, direction, meaningful, created):
    a = ActivityLog(
        activity_type=ActivityType.EMAIL_RECEIVED,
        channel=Channel.EMAIL,
        company_id=company_id,
        customer_site_id=customer_site_id,
        site_contact_id=site_contact_id,
        direction=direction,
        is_meaningful=meaningful,
        created_at=created,
        occurred_at=created,
    )
    db.add(a)
    db.flush()
    return a


def test_materialize_sets_outbound_and_meaningful_reply(db_session):
    co = Company(name="Mat Co")
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(customer_site_id=site.id, full_name="Reply Person")
    db_session.add(contact)
    db_session.flush()

    _log(
        db_session,
        company_id=co.id,
        customer_site_id=site.id,
        site_contact_id=contact.id,
        direction=Direction.OUTBOUND,
        meaningful=None,
        created=NOW - timedelta(days=5),
    )
    _log(
        db_session,
        company_id=co.id,
        customer_site_id=site.id,
        site_contact_id=contact.id,
        direction=Direction.INBOUND,
        meaningful=True,
        created=NOW - timedelta(days=2),
    )
    # noise: inbound but NOT meaningful — must NOT set the reply clock
    _log(
        db_session,
        company_id=co.id,
        customer_site_id=site.id,
        site_contact_id=contact.id,
        direction=Direction.INBOUND,
        meaningful=False,
        created=NOW,
    )
    db_session.commit()

    materialize_company_clocks(db_session, co.id)
    db_session.commit()
    db_session.refresh(co)
    db_session.refresh(contact)
    db_session.refresh(site)

    assert co.last_outbound_at == NOW - timedelta(days=5)
    assert co.last_reply_at == NOW - timedelta(days=2)  # noise ignored
    assert contact.last_outbound_at == NOW - timedelta(days=5)
    assert contact.last_reply_at == NOW - timedelta(days=2)
    assert site.last_outbound_at == NOW - timedelta(days=5)
    assert site.last_reply_at == NOW - timedelta(days=2)


def test_materialize_leaves_clocks_null_when_no_activity(db_session):
    co = Company(name="Quiet Co")
    db_session.add(co)
    db_session.commit()
    materialize_company_clocks(db_session, co.id)
    db_session.commit()
    db_session.refresh(co)
    assert co.last_outbound_at is None and co.last_reply_at is None


def test_materialize_all_clocks_returns_company_count(db_session):
    from app.services.cadence_service import materialize_all_clocks

    co1 = Company(name="All Co 1")
    co2 = Company(name="All Co 2")
    db_session.add_all([co1, co2])
    db_session.flush()
    _log(
        db_session,
        company_id=co1.id,
        direction=Direction.OUTBOUND,
        meaningful=None,
        created=NOW - timedelta(days=3),
    )
    db_session.commit()

    n = materialize_all_clocks(db_session)
    db_session.commit()
    db_session.refresh(co1)
    assert n == 2
    assert co1.last_outbound_at == NOW - timedelta(days=3)

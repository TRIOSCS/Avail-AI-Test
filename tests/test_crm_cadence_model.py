from datetime import UTC, datetime

from app.models.crm import Company, CustomerSite, SiteContact


def test_clock_and_tier_columns_persist(db_session):
    now = datetime(2026, 6, 17, tzinfo=UTC)
    co = Company(name="Clock Co", tier="key", last_outbound_at=now, last_reply_at=now)
    db_session.add(co)
    db_session.commit()
    site = CustomerSite(company_id=co.id, site_name="HQ", last_outbound_at=now, last_reply_at=now)
    db_session.add(site)
    db_session.commit()
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Pat Buyer",
        last_activity_at=now,
        last_outbound_at=now,
        last_reply_at=now,
    )
    db_session.add(contact)
    db_session.commit()
    db_session.refresh(co)
    db_session.refresh(contact)
    assert co.tier == "key"
    assert co.last_outbound_at == now and co.last_reply_at == now
    assert contact.last_activity_at == now and contact.last_outbound_at == now

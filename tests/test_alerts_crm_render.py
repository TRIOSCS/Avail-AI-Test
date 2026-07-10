"""Server-side render test for the CRM inbound spotlight markers.

A new, unseen inbound customer communication on an account the user owns makes that
account's CDM list row render the data-alert-* spotlight attributes.
"""

from datetime import UTC, datetime

from app.constants import ActivityType, Channel, Direction, EventType
from app.models.intelligence import ActivityLog


def _inbound(db, company, user, ext: str) -> ActivityLog:
    row = ActivityLog(
        user_id=user.id,
        activity_type=ActivityType.EMAIL_RECEIVED,
        channel=Channel.EMAIL,
        direction=Direction.INBOUND,
        event_type=EventType.EMAIL,
        company_id=company.id,
        contact_email="buyer@acme.test",
        external_id=ext,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_crm_account_row_gets_spotlight_attrs(client, db_session, test_user, test_company):
    test_company.account_type = "Customer"
    test_company.account_owner_id = test_user.id
    db_session.commit()
    activity = _inbound(db_session, test_company, test_user, "crm-msg-1")

    r = client.get("/v2/partials/customers/account-list")
    assert r.status_code == 200
    body = r.text
    assert "data-alert-new" in body
    assert 'data-alert-kind="inbound_customer"' in body
    assert f'data-alert-refs="{activity.id}"' in body

"""Tests for HTMX enhanced activity timeline and activity logging (Phase 2C).

Covers activity tab rendering with RFQ contacts, activity logs, and manual logging form.

Called by: pytest
Depends on: conftest (client, db_session, test_user fixtures), app.models
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Requisition, User
from app.models.intelligence import ActivityLog
from app.models.offers import Contact as RfqContact


@pytest.fixture()
def req_with_activity(db_session: Session, test_user: User):
    """Create a requisition with RFQ contacts and activity logs."""
    company = Company(name="Activity Co", created_at=datetime.now(timezone.utc))
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="Activity Test Req",
        status="active",
        created_by=test_user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    # RFQ contacts
    contact1 = RfqContact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Vendor A",
        vendor_name_normalized="vendor a",
        vendor_contact="sales@vendora.com",
        subject="RFQ - LM317T",
        status="sent",
        created_at=datetime.now(timezone.utc),
    )
    contact2 = RfqContact(
        requisition_id=req.id,
        user_id=test_user.id,
        contact_type="email",
        vendor_name="Vendor B",
        vendor_name_normalized="vendor b",
        vendor_contact="quotes@vendorb.com",
        subject="RFQ - LM317T",
        status="responded",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([contact1, contact2])

    # Activity log
    activity = ActivityLog(
        user_id=test_user.id,
        requisition_id=req.id,
        activity_type="phone_call",
        channel="phone",
        contact_name="John at Vendor A",
        notes="Called about pricing",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(activity)
    db_session.commit()

    return {"req": req, "contacts": [contact1, contact2], "activity": activity}


def test_activity_tab_renders_rfq_contacts(client, req_with_activity):
    """Activity tab shows RFQ contact history."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    assert resp.status_code == 200
    html = resp.text
    assert "Vendor A" in html
    assert "Vendor B" in html
    assert "sales@vendora.com" in html


def test_activity_tab_shows_status_badges(client, req_with_activity):
    """Activity tab shows status badges on RFQ contacts."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    html = resp.text
    assert "Sent" in html
    assert "Responded" in html


def test_activity_tab_renders_activity_logs(client, req_with_activity):
    """Activity tab shows manually logged activities."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    html = resp.text
    assert "Phone Call" in html
    assert "Called about pricing" in html


def test_activity_tab_shows_summary_counts(client, req_with_activity):
    """Activity tab has summary bar with counts."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    html = resp.text
    assert "RFQs Sent" in html
    assert "Responses" in html
    assert "Activities" in html


def test_activity_tab_has_log_form(client, req_with_activity):
    """Activity tab has a log activity form."""
    req = req_with_activity["req"]
    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    html = resp.text
    assert "Log Activity" in html
    assert f'hx-post="/v2/partials/requisitions/{req.id}/log-activity"' in html
    assert 'name="activity_type"' in html
    assert 'name="notes"' in html


def test_activity_tab_empty_state(client, db_session, test_user):
    """Empty activity tab shows placeholder."""
    company = Company(name="Empty Co", created_at=datetime.now(timezone.utc))
    db_session.add(company)
    db_session.flush()

    site = CustomerSite(company_id=company.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="Empty Req",
        status="active",
        created_by=test_user.id,
        customer_site_id=site.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    resp = client.get(f"/v2/partials/requisitions/{req.id}/tab/activity")
    assert resp.status_code == 200
    assert "No activity recorded" in resp.text


def test_log_activity_creates_record(client, db_session, req_with_activity):
    """POST log-activity creates an ActivityLog and returns refreshed tab."""
    req = req_with_activity["req"]
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/log-activity",
        data={
            "activity_type": "note",
            "vendor_name": "Test Vendor",
            "notes": "Left a voicemail",
        },
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Left a voicemail" in html

    # Verify record was created
    logs = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == req.id,
            ActivityLog.notes == "Left a voicemail",
        )
        .all()
    )
    assert len(logs) == 1
    assert logs[0].activity_type == "note"
    assert logs[0].channel == "note"


def test_log_activity_phone_call(client, db_session, req_with_activity):
    """Logging a phone call sets correct channel."""
    req = req_with_activity["req"]
    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/log-activity",
        data={
            "activity_type": "phone_call",
            "vendor_name": "Call Vendor",
            "contact_phone": "+15559876543",
            "notes": "Discussed pricing",
        },
    )
    assert resp.status_code == 200

    log = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.notes == "Discussed pricing",
        )
        .first()
    )
    assert log.channel == "phone"
    assert log.contact_phone == "+15559876543"


def test_log_activity_404_for_missing_req(client):
    """Log activity returns 404 for non-existent requisition."""
    resp = client.post(
        "/v2/partials/requisitions/99999/log-activity",
        data={"activity_type": "note", "notes": "test"},
    )
    assert resp.status_code == 404

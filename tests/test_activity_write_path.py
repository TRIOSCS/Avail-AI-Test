"""test_activity_write_path.py — Tests for the unified activity write path.

Covers the ActivityType enum, log_activity() canonical writer, the
log_rfq_activity() delegating alias, requisition_id on email/call logging,
and get_requisition_activities().

Called by: pytest
Depends on: app/constants.py, app/services/activity_service.py, conftest.py
"""

from app.constants import ActivityType
from app.jobs.email_jobs import _AVAIL_TAG_RE
from app.models import ActivityLog
from app.services.activity_service import (
    get_requisition_activities,
    log_activity,
    log_call_activity,
    log_email_activity,
    log_rfq_activity,
)


def test_activity_type_values_fit_column():
    """Every canonical activity_type value fits the activity_log.activity_type
    String(20) column."""
    for member in ActivityType:
        assert len(member.value) <= 20, f"{member.value} exceeds 20 chars"


def test_activity_type_has_expected_members():
    assert ActivityType.RFQ_SENT == "rfq_sent"
    assert ActivityType.STATUS_CHANGED == "status_changed"
    assert ActivityType.OFFER_STATUS_CHANGED == "offer_status_changed"


def test_log_activity_sets_requisition_id(db_session, test_requisition, test_user):
    record = log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        channel="system",
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="Status changed from active to sourcing",
    )
    assert record.id is not None
    assert record.requisition_id == test_requisition.id
    assert record.activity_type == "status_changed"
    assert record.channel == "system"
    assert record.notes == "Status changed from active to sourcing"


def test_log_rfq_activity_delegates_to_log_activity(db_session, test_requisition, test_user):
    record = log_rfq_activity(
        db=db_session,
        rfq_id=test_requisition.id,
        activity_type="status_change",
        description="legacy call path",
        user_id=test_user.id,
    )
    assert record.requisition_id == test_requisition.id
    assert record.notes == "legacy call path"
    rows = db_session.query(ActivityLog).filter_by(requisition_id=test_requisition.id).all()
    assert len(rows) == 1


def test_log_email_activity_accepts_requisition_id(db_session, test_requisition, test_user):
    record = log_email_activity(
        user_id=test_user.id,
        direction="sent",
        email_addr="vendor@example.com",
        subject="RFQ [ref:%d]" % test_requisition.id,
        external_id="msg-req-001",
        contact_name="Vendor Rep",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.requisition_id == test_requisition.id


def test_log_call_activity_accepts_requisition_id(db_session, test_requisition, test_user):
    record = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15551234567",
        duration_seconds=120,
        external_id="call-req-001",
        contact_name="Vendor Rep",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.requisition_id == test_requisition.id


def test_avail_tag_re_matches_ref_format():
    """The sent-folder scan must recognise the [ref:N] tag that RFQ send writes."""
    assert _AVAIL_TAG_RE.search("Quote request RE part [ref:4321]").group(1) == "4321"


def test_avail_tag_re_matches_legacy_format():
    assert _AVAIL_TAG_RE.search("Quote request [AVAIL-99]").group(1) == "99"


def test_get_requisition_activities_returns_scoped_rows(db_session, test_requisition, test_user):
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="first",
    )
    log_activity(
        db_session,
        activity_type=ActivityType.RFQ_SENT,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="second",
    )
    rows = get_requisition_activities(test_requisition.id, db_session)
    assert len(rows) == 2
    assert all(r.requisition_id == test_requisition.id for r in rows)


def test_get_requisition_activities_excludes_other_reqs(db_session, test_requisition, test_user):
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="mine",
    )
    assert get_requisition_activities(999999, db_session) == []


def test_activity_tab_renders_logged_event(client, db_session, test_requisition, test_user):
    """An event written via log_activity() appears on the requisition Activity tab."""
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="Status changed from active to sourcing",
    )
    db_session.commit()
    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/activity")
    assert resp.status_code == 200
    assert "Status changed from active to sourcing" in resp.text
    assert "No activity recorded yet" not in resp.text

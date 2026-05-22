"""test_activity_write_path.py — Tests for the unified activity write path.

Covers the ActivityType enum, log_activity() canonical writer, the
log_rfq_activity() delegating alias, requisition_id on email/call logging,
and get_requisition_activities().

Called by: pytest
Depends on: app/constants.py, app/services/activity_service.py, conftest.py
"""

from app.constants import ActivityType
from app.models import ActivityLog
from app.services.activity_service import (
    get_requisition_activities,
    log_activity,
    log_call_activity,
    log_email_activity,
    log_rfq_activity,
)
from app.shared_constants import RFQ_SUBJECT_TAG_RE


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
        subject=f"RFQ [ref:{test_requisition.id}]",
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
    assert RFQ_SUBJECT_TAG_RE.search("Quote request RE part [ref:4321]").group(1) == "4321"


def test_log_email_activity_accepts_none_user(db_session, test_requisition):
    """log_email_activity tolerates user_id=None (userless inbox scan)."""
    record = log_email_activity(
        user_id=None,
        direction="received",
        email_addr="vendor@example.com",
        subject="RE: RFQ",
        external_id="msg-none-user-001",
        contact_name="Vendor Rep",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.user_id is None
    assert record.requisition_id == test_requisition.id


def test_avail_tag_re_matches_legacy_format():
    assert RFQ_SUBJECT_TAG_RE.search("Quote request [AVAIL-99]").group(1) == "99"


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


def test_log_activity_resolves_company_from_requisition(db_session, test_user, test_company, test_customer_site):
    """log_activity() backfills company_id by walking requisition -> site -> company."""
    from app.models.sourcing import Requisition

    req = Requisition(
        name="REQ-COMPANY-RESOLVE",
        customer_name="Acme Electronics",
        status="active",
        created_by=test_user.id,
        customer_site_id=test_customer_site.id,
    )
    db_session.add(req)
    db_session.flush()

    record = log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=req.id,
        user_id=test_user.id,
        description="resolves company",
    )
    assert record.company_id == test_company.id


def test_log_activity_explicit_company_id_skips_resolution(db_session, test_user, test_company, test_customer_site):
    """An explicitly passed company_id is used as-is, not overwritten by resolution."""
    from app.models.sourcing import Requisition

    req = Requisition(
        name="REQ-EXPLICIT-COMPANY",
        customer_name="Acme Electronics",
        status="active",
        created_by=test_user.id,
        customer_site_id=test_customer_site.id,
    )
    db_session.add(req)
    db_session.flush()

    record = log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=req.id,
        company_id=test_company.id,
        user_id=test_user.id,
        description="explicit company",
    )
    assert record.company_id == test_company.id


def test_log_rfq_activity_maps_metadata_to_details(db_session, test_requisition, test_user):
    """The alias maps its `metadata` arg onto the `details` column and forces
    channel=system."""
    record = log_rfq_activity(
        db=db_session,
        rfq_id=test_requisition.id,
        activity_type="status_change",
        description="with metadata",
        metadata={"old": "active", "new": "sourcing"},
        user_id=test_user.id,
    )
    assert record.details == {"old": "active", "new": "sourcing"}
    assert record.channel == "system"


def _make_inbox_message(req_id):
    """Build a single fake Graph inbox message tagged for the given requisition."""
    return {
        "id": "poll-inbox-msg-001",
        "subject": f"RE: Quote request [AVAIL-{req_id}]",
        "from": {
            "emailAddress": {
                "address": "vendor@parts.com",
                "name": "Vendor Rep",
            }
        },
        "bodyPreview": "Quote attached",
        "body": {"content": "<p>Quote attached</p>"},
        "conversationId": "poll-inbox-conv-001",
        "receivedDateTime": None,
    }


async def test_poll_inbox_logs_email_received_activity(db_session, test_requisition, test_user):
    """Driving the real poll_inbox with one inbound vendor reply writes exactly one
    email_received ActivityLog row for the matched requisition."""
    from unittest.mock import AsyncMock, patch

    from app.email_service import poll_inbox

    mock_gc = AsyncMock()
    mock_gc.get_json.return_value = {"value": [_make_inbox_message(test_requisition.id)]}

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.email_service.get_credential_cached", return_value=None),
    ):
        await poll_inbox(
            token="fake-token",
            db=db_session,
            requisition_id=test_requisition.id,
        )

    rows = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == test_requisition.id,
            ActivityLog.activity_type == "email_received",
        )
        .all()
    )
    assert len(rows) == 1


def test_log_activity_marks_meaningful_types(db_session, test_requisition, test_user):
    """Inherently-meaningful activity types are flagged is_meaningful=True at write
    time."""
    rec = log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="status changed",
    )
    assert rec.is_meaningful is True


def test_log_activity_leaves_ai_scored_types_unflagged(db_session, test_requisition, test_user):
    """AI-scored types (sighting_added) are left is_meaningful=None for the quality
    pass."""
    rec = log_activity(
        db_session,
        activity_type=ActivityType.SIGHTING_ADDED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="12 sightings added",
    )
    assert rec.is_meaningful is None

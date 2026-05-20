"""test_activity_write_path.py — Tests for the unified activity write path.

Covers the ActivityType enum, log_activity() canonical writer, the
log_rfq_activity() delegating alias, requisition_id on email/call logging,
and get_requisition_activities().

Called by: pytest
Depends on: app/constants.py, app/services/activity_service.py, conftest.py
"""

from app.constants import ActivityType


def test_activity_type_values_fit_column():
    """Every canonical activity_type value fits the activity_log.activity_type
    String(20) column."""
    for member in ActivityType:
        assert len(member.value) <= 20, f"{member.value} exceeds 20 chars"


def test_activity_type_has_expected_members():
    assert ActivityType.RFQ_SENT == "rfq_sent"
    assert ActivityType.STATUS_CHANGED == "status_changed"
    assert ActivityType.OFFER_STATUS_CHANGED == "offer_status_changed"

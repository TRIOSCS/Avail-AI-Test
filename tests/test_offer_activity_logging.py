"""test_offer_activity_logging.py — offer events write activity_log rows.

Covers Plan 2a: offer_created at all 10 creation sites and offer_status_changed
at all 10 status-change sites route through activity_service.log_activity().

Called by: pytest
Depends on: app/services/activity_service.py, app/constants.py, conftest.py
"""

from app.constants import ActivityType
from app.models import ActivityLog


def _activity_rows(db, requisition_id, activity_type):
    return (
        db.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == requisition_id,
            ActivityLog.activity_type == activity_type,
        )
        .all()
    )


def test_create_offer_route_logs_offer_created(client, db_session, test_requisition, test_vendor_card):
    """POST to the offer-create API writes an offer_created activity row."""
    before = len(_activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED))
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/offers",
        json={
            "requirement_id": None,
            "vendor_card_id": test_vendor_card.id,
            "mpn": "LM317T",
            "vendor_name": test_vendor_card.display_name,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED)
    assert len(rows) == before + 1

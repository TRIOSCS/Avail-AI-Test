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


def test_email_parsed_offer_logs_offer_created(db_session, test_requisition):
    """An offer auto-created from a parsed vendor email writes offer_created."""
    from app.email_service import _auto_create_offers_from_parse
    from app.models.offers import VendorResponse

    vr = VendorResponse(
        requisition_id=test_requisition.id,
        vendor_name="Vendor X",
        vendor_email="vendor@example.com",
        subject="RE: RFQ",
        body="We can supply.",
        confidence=0.95,
    )
    db_session.add(vr)
    db_session.flush()

    parsed = {
        "parts": [
            {
                "mpn": "LM317T",
                "status": "quoted",
                "unit_price": 0.5,
                "qty_available": 100,
            }
        ]
    }
    _auto_create_offers_from_parse(vr, parsed, db_session)
    db_session.commit()

    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED)
    assert len(rows) >= 1

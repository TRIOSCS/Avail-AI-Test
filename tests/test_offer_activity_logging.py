"""test_offer_activity_logging.py — offer events write activity_log rows.

Covers Plan 2a: offer_created at all 10 creation sites and offer_status_changed
at all 10 status-change sites route through activity_service.log_activity().

Called by: pytest
Depends on: app/services/activity_service.py, app/constants.py, conftest.py
"""

import pytest

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


def test_email_parsed_offer_logs_offer_created(db_session, test_requisition):
    """An offer auto-created from a parsed vendor email writes offer_created."""
    from app.models.offers import VendorResponse
    from app.services.offer_service import create_offers_from_parsed_response

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
    create_offers_from_parsed_response(vr, parsed, db_session)
    db_session.commit()

    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED)
    assert len(rows) >= 1


# The save_parsed_offers / save_freeform_offers JSON-pair tests were deleted in W3
# together with the functions themselves (their /api/ai routes died in the W2 sweep;
# the surviving HTMX form path is covered by test_save_parsed_offers_normalize_qual.py
# and test_htmx_views.py).


def test_clone_requisition_logs_offer_created(db_session, test_requisition, test_user, test_offer):
    """Cloning a requisition that has offers logs offer_created per cloned offer."""
    from app.services.requisition_service import clone_requisition

    before = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.OFFER_CREATED).count()
    new_req = clone_requisition(db=db_session, source_req=test_requisition, user_id=test_user.id)
    db_session.commit()
    after = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.OFFER_CREATED).count()
    assert after > before
    rows = _activity_rows(db_session, new_req.id, ActivityType.OFFER_CREATED)
    assert len(rows) >= 1


def test_approve_offer_logs_status_changed(client, db_session, test_requisition, test_offer):
    """Approving an offer via the API writes an offer_status_changed activity row."""
    test_offer.status = "pending_review"
    db_session.commit()
    resp = client.put(f"/api/offers/{test_offer.id}/approve")
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_STATUS_CHANGED)
    assert len(rows) == 1
    assert "status:" in (rows[0].notes or "")
    assert rows[0].details["old_status"] == "pending_review"
    assert rows[0].details["new_status"] == "active"
    assert rows[0].details["offer_id"] == test_offer.id


def test_add_offer_htmx_logs_offer_created(client, db_session, test_requisition):
    """The add-offer HTMX route writes exactly one offer_created activity row."""
    before = len(_activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED))
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/add-offer",
        data={"vendor_name": "Arrow Electronics", "mpn": "LM317T"},
    )
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED)
    assert len(rows) == before + 1


@pytest.mark.parametrize(
    ("method", "url", "data", "initial_status"),
    [
        pytest.param("put", "/api/offers/{offer}/reject", None, "pending_review", id="api_reject"),
        pytest.param("post", "/api/offers/{offer}/reject", None, "pending_review", id="api_reject_t4_review"),
        pytest.param(
            "post",
            "/v2/partials/requisitions/{req}/offers/{offer}/review",
            {"action": "reject"},
            "pending_review",
            id="htmx_review_reject",
        ),
        pytest.param(
            "post",
            "/v2/partials/requisitions/{req}/offers/{offer}/mark-sold",
            None,
            "active",
            id="htmx_mark_sold",
        ),
        pytest.param("post", "/v2/partials/offers/{offer}/promote", None, "pending_review", id="htmx_promote"),
        pytest.param("post", "/v2/partials/offers/{offer}/reject", None, "pending_review", id="htmx_reject"),
    ],
)
def test_status_change_route_logs_status_changed(
    client, db_session, test_requisition, test_offer, method, url, data, initial_status
):
    """Each offer status-change route writes exactly one offer_status_changed activity
    row."""
    test_offer.status = initial_status
    db_session.commit()
    before = len(_activity_rows(db_session, test_requisition.id, ActivityType.OFFER_STATUS_CHANGED))
    resp = getattr(client, method)(
        url.format(req=test_requisition.id, offer=test_offer.id),
        data=data,
    )
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_STATUS_CHANGED)
    assert len(rows) == before + 1


def test_review_offer_htmx_logs_status_changed(client, db_session, test_requisition, test_offer):
    """Approving an offer through the HTMX review handler logs offer_status_changed."""
    test_offer.status = "pending_review"
    db_session.commit()
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition.id}/offers/{test_offer.id}/review",
        data={"action": "approve"},
    )
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_STATUS_CHANGED)
    assert len(rows) >= 1

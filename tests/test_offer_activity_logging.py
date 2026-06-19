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


def _one_parsed_offer():
    """Return an offers list with one valid DraftOfferItem for the AI offer services."""
    from app.schemas.ai import DraftOfferItem

    return [
        DraftOfferItem(
            vendor_name="Arrow Electronics",
            mpn="LM317T",
            manufacturer="Texas Instruments",
            qty_available=500,
            unit_price=0.42,
        )
    ]


def test_save_parsed_offers_logs_offer_created(db_session, test_requisition):
    """save_parsed_offers writes one offer_created row per saved offer."""
    from app.services.ai_offer_service import save_parsed_offers

    save_parsed_offers(
        db=db_session,
        requisition_id=test_requisition.id,
        response_id=None,
        offers=_one_parsed_offer(),
        user_id=None,
    )
    db_session.commit()
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED)
    assert len(rows) >= 1


def test_save_freeform_offers_logs_offer_created(db_session, test_requisition, test_user):
    """save_freeform_offers writes one offer_created row per saved offer."""
    from app.services.ai_offer_service import save_freeform_offers

    save_freeform_offers(
        db=db_session,
        requisition_id=test_requisition.id,
        offers=_one_parsed_offer(),
        user_id=test_user.id,
    )
    db_session.commit()
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_CREATED)
    assert len(rows) >= 1


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
        pytest.param("patch", "/api/offers/{offer}/mark-sold", None, "active", id="api_mark_sold"),
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


def test_promote_offer_logs_status_changed(client, db_session, test_requisition, test_offer):
    """POST /api/offers/{id}/promote writes one offer_status_changed row with a real
    status change."""
    test_offer.status = "pending_review"
    test_offer.evidence_tier = "T4"
    db_session.commit()
    before = len(_activity_rows(db_session, test_requisition.id, ActivityType.OFFER_STATUS_CHANGED))
    resp = client.post(f"/api/offers/{test_offer.id}/promote")
    assert resp.status_code == 200, resp.text
    rows = _activity_rows(db_session, test_requisition.id, ActivityType.OFFER_STATUS_CHANGED)
    assert len(rows) == before + 1
    assert rows[0].details["old_status"] != rows[0].details["new_status"]


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


# ═══════════════════════════════════════════════════════════════════════
#  _upsert_inapp_notice — dedup helper (create + refresh branches)
# ═══════════════════════════════════════════════════════════════════════


def test_upsert_inapp_notice_creates_then_refreshes(db_session, test_requisition, test_user):
    """First call inserts a system-channel ActivityLog; second refreshes in place."""
    from app.routers.crm.offers import _upsert_inapp_notice

    def _rows():
        return (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.user_id == test_user.id,
                ActivityLog.activity_type == "new_offer",
                ActivityLog.requisition_id == test_requisition.id,
                ActivityLog.dismissed_at.is_(None),
            )
            .all()
        )

    _upsert_inapp_notice(
        db_session,
        user_id=test_user.id,
        activity_type="new_offer",
        requisition_id=test_requisition.id,
        contact_name="Acme Vendor",
        subject="New offer: Acme Vendor — LM317T",
    )
    db_session.commit()
    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "system"
    assert row.contact_name == "Acme Vendor"
    assert row.subject == "New offer: Acme Vendor — LM317T"

    # Second call with the same key updates subject in place (no new row)
    _upsert_inapp_notice(
        db_session,
        user_id=test_user.id,
        activity_type="new_offer",
        requisition_id=test_requisition.id,
        contact_name="Acme Vendor",
        subject="New offer: Acme Vendor — LM317T · 3 total offers",
    )
    db_session.commit()
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].id == row.id
    assert rows[0].subject == "New offer: Acme Vendor — LM317T · 3 total offers"

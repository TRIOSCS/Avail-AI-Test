"""Regression tests: requisition-ownership IDOR guards in app/routers/sightings.py.

A SALES/TRADER user may only act on requisitions they created. Each test flips the
shared test user's role to SALES and re-owns the requisition to someone else
(admin_user), then asserts the mutating/sending endpoint returns 404 (existence not
leaked) instead of acting on a non-owned requisition.

Covers every HIGH-severity sightings endpoint plus the offer-scoped mark-sold mutation.
"""

import pytest
from sqlalchemy.orm import Session

from app.constants import OfferStatus, UserRole
from app.models import User
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition


def _requirement(db: Session, req: Requisition) -> Requirement:
    """The single requirement seeded under the test_requisition fixture."""
    return db.query(Requirement).filter(Requirement.requisition_id == req.id).first()


def _offer(db: Session, req: Requisition, requirement: Requirement) -> Offer:
    """Create a pending_review offer on the requisition/requirement."""
    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        vendor_name="Acme Parts",
        mpn="LM317T",
        status=OfferStatus.PENDING_REVIEW,
        source="manual",
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


@pytest.fixture()
def _as_sales_nonowner(db_session: Session, test_requisition: Requisition, test_user: User, admin_user: User):
    """Flip the request user to SALES and re-own the requisition to admin_user."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()
    return test_requisition


# ── requisition-by-path-derived (via requirement) HIGH-severity endpoints ──────


def test_advance_status_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    resp = client.patch(
        f"/v2/partials/sightings/{requirement.id}/advance-status",
        data={"status": "sourcing"},
    )
    assert resp.status_code == 404


def test_create_offer_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/offers",
        data={"vendor_name": "Acme", "mpn": "LM317T"},
    )
    assert resp.status_code == 404


def test_review_offer_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    offer = _offer(db_session, req, requirement)
    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/offers/{offer.id}/review",
        data={"action": "approve"},
    )
    assert resp.status_code == 404


def test_update_offer_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    offer = _offer(db_session, req, requirement)
    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/offers/{offer.id}",
        data={"vendor_name": "Acme", "mpn": "LM317T"},
    )
    assert resp.status_code == 404


def test_delete_offer_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    offer = _offer(db_session, req, requirement)
    resp = client.delete(f"/v2/partials/sightings/{requirement.id}/offers/{offer.id}")
    assert resp.status_code == 404


def test_mark_offer_sold_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    offer = _offer(db_session, req, requirement)
    resp = client.post(f"/v2/partials/sightings/{requirement.id}/offers/{offer.id}/mark-sold")
    assert resp.status_code == 404


# ── offer-loaded (no requirement load) HIGH-severity request-send endpoint ─────


def test_offer_request_send_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    offer = _offer(db_session, req, requirement)
    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/offers/{offer.id}/request/0/send",
    )
    assert resp.status_code == 404


def test_offer_request_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    offer = _offer(db_session, req, requirement)
    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/offers/{offer.id}/request",
        data={"kind": "images"},
    )
    assert resp.status_code == 404


# ── send-inquiry (basket of requirements) HIGH-severity endpoint ───────────────


def test_send_inquiry_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    resp = client.post(
        "/v2/partials/sightings/send-inquiry",
        data={
            "requirement_ids": [str(requirement.id)],
            "vendor_names": ["Acme"],
            "email_body": "please quote",
        },
    )
    assert resp.status_code == 404


# ── MEDIUM/LOW requirement-scoped mutations also covered ───────────────────────


def test_mark_unavailable_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/mark-unavailable",
        data={"vendor_name": "Acme", "reason": "sold_elsewhere"},
    )
    assert resp.status_code == 404


def test_log_activity_blocks_non_owner_sales(client, db_session, _as_sales_nonowner):
    req = _as_sales_nonowner
    requirement = _requirement(db_session, req)
    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/log-activity",
        data={"notes": "called vendor", "channel": "note"},
    )
    assert resp.status_code == 404


# ── happy path: buyer (unrestricted) still allowed ─────────────────────────────


def test_buyer_owner_advance_status_allowed(client, db_session, test_requisition):
    """A buyer (default test_user role) is unrestricted — the guard is a no-op."""
    requirement = _requirement(db_session, test_requisition)
    resp = client.patch(
        f"/v2/partials/sightings/{requirement.id}/advance-status",
        data={"status": "sourcing"},
    )
    # Not a 404 from the ownership guard (transition validity aside).
    assert resp.status_code != 404

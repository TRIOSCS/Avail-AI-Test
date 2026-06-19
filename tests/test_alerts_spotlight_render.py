"""Server-side render test for the Phase 1 spotlight markers.

Verifies the route → markers_for_tab → _alert_macros path: a new confirmed offer on the
user's requirement makes its Sales Hub row render the data-alert-* spotlight attributes,
and a seen offer does not. (The glide/observe/seen JS behaviour is browser-only —
covered separately by the Playwright spec.)
"""

from datetime import datetime, timezone

from app.constants import AlertKind, OfferStatus, QualificationStatus
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.services.alerts import record_seen


def _requirement_of(db, req: Requisition) -> Requirement:
    return db.query(Requirement).filter(Requirement.requisition_id == req.id).first()


def _make_confirmed_offer(db, requirement: Requirement) -> Offer:
    offer = Offer(
        requisition_id=requirement.requisition_id,
        requirement_id=requirement.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        status=OfferStatus.APPROVED,
        qualification_status=QualificationStatus.COMPLETE,
        approved_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


def test_sales_hub_row_gets_spotlight_attrs(client, db_session, test_user, test_requisition):
    requirement = _requirement_of(db_session, test_requisition)
    offer = _make_confirmed_offer(db_session, requirement)

    r = client.get("/v2/partials/parts")
    assert r.status_code == 200
    body = r.text
    assert "data-alert-new" in body
    assert 'data-alert-kind="offer_confirmed"' in body
    assert 'data-alert-temperament="fyi"' in body
    assert f'data-alert-refs="{offer.id}"' in body


def test_sales_hub_no_spotlight_when_seen(client, db_session, test_user, test_requisition):
    requirement = _requirement_of(db_session, test_requisition)
    offer = _make_confirmed_offer(db_session, requirement)
    record_seen(db_session, test_user, AlertKind.OFFER_CONFIRMED, offer.id)

    r = client.get("/v2/partials/parts")
    assert r.status_code == 200
    assert "data-alert-new" not in r.text  # seen → drained → no spotlight

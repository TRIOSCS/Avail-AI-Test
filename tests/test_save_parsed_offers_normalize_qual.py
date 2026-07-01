"""test_save_parsed_offers_normalize_qual.py — regression for the AI-parse save path.

Covers the save_parsed_offers HTMX route (POST
/v2/partials/requisitions/{req_id}/save-parsed-offers): offers saved from a reviewed
AI parse must store the canonical key-form ``normalized_mpn`` and have a computed
``qualification_status`` so they are visible in the part-centric Offers panel
(part_offers_for) and carry qualification state — exactly like add_offer.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.constants import RequisitionStatus
from app.models import Offer, Requirement, Requisition, User
from app.services.part_offers import part_offers_for
from app.utils.normalization import normalize_mpn_key


def _make_requisition(db: Session, user: User, primary_mpn: str) -> Requisition:
    req = Requisition(
        name="REQ-PARSE-QUAL-001",
        customer_name="Test Customer",
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    db.add(
        Requirement(
            requisition_id=req.id,
            primary_mpn=primary_mpn,
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    db.refresh(req)
    return req


def test_save_parsed_offer_sets_key_form_normalized_mpn_and_qualification(client, db_session, test_user):
    """A saved AI-parsed offer gets a key-form normalized_mpn and a computed status."""
    # Dashed/dotted MPN proves we store the KEY form (dashes+dots stripped), not display.
    mpn = "LM2596S-5.0"
    req = _make_requisition(db_session, test_user, primary_mpn=mpn)

    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
        data={
            "vendor_name": "Arrow Electronics",
            "offers[0].mpn": mpn,
            "offers[0].qty_available": "500",
            "offers[0].unit_price": "0.85",
            "offers[0].condition": "refurb",
        },
    )
    assert resp.status_code == 200

    offer = db_session.query(Offer).filter(Offer.requisition_id == req.id).one()
    # normalized_mpn must be the canonical dedup key (dash/dot stripped, lowercased).
    assert offer.normalized_mpn == normalize_mpn_key(mpn) == "lm2596s50"
    # apply_qualification must have run: status is computed (not NULL) for refurb.
    assert offer.qualification_status is not None
    assert offer.qualification_status != "unset"


def test_save_parsed_offer_visible_in_part_offers_for(client, db_session, test_user):
    """The saved offer matches via normalized_mpn so it shows in the part Offers
    panel."""
    mpn = "LM317T"
    req = _make_requisition(db_session, test_user, primary_mpn=mpn)

    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/save-parsed-offers",
        data={
            "vendor_name": "Arrow Electronics",
            "offers[0].mpn": mpn,
            "offers[0].qty_available": "250",
            "offers[0].condition": "new",
        },
    )
    assert resp.status_code == 200

    requirement = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).one()
    found = part_offers_for(requirement, db_session)
    assert any(o.mpn == mpn and o.normalized_mpn == normalize_mpn_key(mpn) for o in found)

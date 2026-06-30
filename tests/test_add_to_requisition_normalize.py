"""Regression tests for add-to-requisition MPN normalization.

What this does: verifies the /v2/partials/search/add-to-requisition endpoint
stores the canonical key-form ``normalized_mpn`` (matching ``normalize_mpn_key``)
and resolves ``material_card_id`` when it auto-creates a Requirement — mirroring
update_requirement so part-history / material-card joins line up.
What calls it: pytest.
Depends on: app.routers.htmx_views.add_to_requisition, conftest fixtures.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus, SourcingStatus
from app.models import MaterialCard, Requirement, Requisition, User
from app.utils.normalization import normalize_mpn_key


def _make_requisition(db: Session, user: User) -> Requisition:
    req = Requisition(
        name="REQ-NORM",
        customer_name="Norm Corp",
        status=RequisitionStatus.OPEN,
        created_by=user.id,
        claimed_by_id=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def test_add_to_requisition_stores_key_form_normalized_mpn(client: TestClient, db_session: Session, test_user: User):
    """A Requirement auto-created via add-to-requisition must store the key-form
    normalized_mpn (lowercase, no separators), not the display form."""
    req = _make_requisition(db_session, test_user)
    db_session.commit()

    raw_mpn = "LM2596S-5.0"
    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        json={
            "requisition_id": req.id,
            "mpn": raw_mpn,
            "items": [
                {
                    "vendor_name": "Arrow",
                    "mpn_matched": raw_mpn,
                    "qty_available": 500,
                    "confidence": 80,
                    "score": 60,
                }
            ],
        },
    )
    assert resp.status_code == 200

    requirement = db_session.query(Requirement).filter_by(requisition_id=req.id, primary_mpn=raw_mpn.upper()).one()
    # Canonical key form: "lm2596s50" — NOT the display/upper form "LM2596S-5.0".
    assert requirement.normalized_mpn == normalize_mpn_key(raw_mpn)
    assert requirement.normalized_mpn == "lm2596s50"


def test_add_to_requisition_resolves_material_card(client: TestClient, db_session: Session, test_user: User):
    """When a matching MaterialCard exists, the auto-created Requirement must get its
    material_card_id populated (resolve_material_card)."""
    req = _make_requisition(db_session, test_user)
    raw_mpn = "LM2596S-5.0"
    card = MaterialCard(
        normalized_mpn=normalize_mpn_key(raw_mpn),
        display_mpn="LM2596S-5.0",
        search_count=0,
    )
    db_session.add(card)
    db_session.commit()
    card_id = card.id

    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        json={
            "requisition_id": req.id,
            "mpn": raw_mpn,
            "items": [
                {
                    "vendor_name": "Mouser",
                    "mpn_matched": raw_mpn,
                    "qty_available": 100,
                    "confidence": 70,
                    "score": 50,
                }
            ],
        },
    )
    assert resp.status_code == 200

    requirement = db_session.query(Requirement).filter_by(requisition_id=req.id, primary_mpn=raw_mpn.upper()).one()
    assert requirement.material_card_id == card_id


def test_add_to_requisition_existing_requirement_left_untouched(
    client: TestClient, db_session: Session, test_user: User
):
    """If the Requirement already exists, no new one is created and the existing
    primary_mpn is reused (sanity around the find-or-create branch)."""
    req = _make_requisition(db_session, test_user)
    existing = Requirement(
        requisition_id=req.id,
        primary_mpn="NE555P",
        normalized_mpn=normalize_mpn_key("NE555P"),
        target_qty=10,
        sourcing_status=SourcingStatus.OPEN,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(existing)
    db_session.commit()

    resp = client.post(
        "/v2/partials/search/add-to-requisition",
        json={
            "requisition_id": req.id,
            "mpn": "NE555P",
            "items": [{"vendor_name": "Mouser", "qty_available": 100, "confidence": 70, "score": 50}],
        },
    )
    assert resp.status_code == 200
    assert db_session.query(Requirement).filter_by(requisition_id=req.id).count() == 1

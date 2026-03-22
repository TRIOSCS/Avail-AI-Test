"""test_manufacturer_validation.py — Tests for manufacturer validation on HTMX entry
paths.

Verifies that add_requirement and update_requirement reject empty manufacturer.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User


@pytest.fixture()
def test_requisition_for_mfr(db_session: Session, test_user: User) -> Requisition:
    """A minimal requisition for manufacturer validation tests."""
    req = Requisition(
        name="Mfr-Test-Req",
        status="active",
        created_by=test_user.id,
        claimed_by_id=test_user.id,
    )
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    return req


def test_add_requirement_rejects_empty_manufacturer(client, test_requisition_for_mfr):
    """Creating a requirement without manufacturer should fail with 422."""
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition_for_mfr.id}/requirements",
        data={
            "primary_mpn": "LM317T",
            "manufacturer": "",
            "target_qty": "100",
        },
    )
    assert resp.status_code == 422 or "manufacturer" in resp.text.lower()


def test_add_requirement_rejects_whitespace_manufacturer(client, test_requisition_for_mfr):
    """Whitespace-only manufacturer should be rejected with 422."""
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition_for_mfr.id}/requirements",
        data={
            "primary_mpn": "LM317T",
            "manufacturer": "   ",
            "target_qty": "100",
        },
    )
    assert resp.status_code == 422 or "manufacturer" in resp.text.lower()


def test_add_requirement_accepts_valid_manufacturer(client, db_session, test_requisition_for_mfr):
    """A requirement with a valid manufacturer should succeed."""
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition_for_mfr.id}/requirements",
        data={
            "primary_mpn": "LM317T",
            "manufacturer": "Texas Instruments",
            "target_qty": "100",
        },
    )
    assert resp.status_code == 200
    item = (
        db_session.query(Requirement)
        .filter(
            Requirement.requisition_id == test_requisition_for_mfr.id,
            Requirement.primary_mpn == "LM317T",
        )
        .first()
    )
    assert item is not None
    assert item.manufacturer == "Texas Instruments"


def test_update_requirement_rejects_empty_manufacturer(client, db_session, test_requisition_for_mfr):
    """Updating a requirement with empty manufacturer should fail with 422."""
    item = Requirement(
        requisition_id=test_requisition_for_mfr.id,
        primary_mpn="NE555",
        manufacturer="Texas Instruments",
        target_qty=50,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    resp = client.put(
        f"/v2/partials/requisitions/{test_requisition_for_mfr.id}/requirements/{item.id}",
        data={
            "primary_mpn": "NE555",
            "manufacturer": "",
            "target_qty": "50",
        },
    )
    assert resp.status_code == 422 or "manufacturer" in resp.text.lower()


def test_update_requirement_accepts_valid_manufacturer(client, db_session, test_requisition_for_mfr):
    """Updating a requirement with a valid manufacturer should succeed."""
    item = Requirement(
        requisition_id=test_requisition_for_mfr.id,
        primary_mpn="NE555P",
        manufacturer="Texas Instruments",
        target_qty=50,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    resp = client.put(
        f"/v2/partials/requisitions/{test_requisition_for_mfr.id}/requirements/{item.id}",
        data={
            "primary_mpn": "NE555P",
            "manufacturer": "ON Semiconductor",
            "target_qty": "75",
        },
    )
    assert resp.status_code == 200
    db_session.refresh(item)
    assert item.manufacturer == "ON Semiconductor"


def test_add_requirement_with_structured_subs(client, db_session, test_requisition_for_mfr):
    """Structured sub_mpn[] / sub_manufacturer[] arrays should be stored on the
    requirement."""
    resp = client.post(
        f"/v2/partials/requisitions/{test_requisition_for_mfr.id}/requirements",
        data={
            "primary_mpn": "LM7805",
            "manufacturer": "Texas Instruments",
            "target_qty": "10",
            "sub_mpn": ["UA7805", "MC7805"],
            "sub_manufacturer": ["Texas Instruments", "ON Semiconductor"],
        },
    )
    assert resp.status_code == 200
    item = (
        db_session.query(Requirement)
        .filter(
            Requirement.requisition_id == test_requisition_for_mfr.id,
            Requirement.primary_mpn == "LM7805",
        )
        .first()
    )
    assert item is not None
    assert isinstance(item.substitutes, list)
    sub_mpns = [s["mpn"] if isinstance(s, dict) else s for s in item.substitutes]
    assert any("UA7805" in m or "ua7805" in m.lower() for m in sub_mpns)

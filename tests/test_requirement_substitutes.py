"""test_requirement_substitutes.py — Requirement substitutes via requisition endpoints.

Tests that POST /v2/partials/requisitions/{id}/requirements and
PUT /v2/partials/requisitions/{id}/requirements/{rid} persist substitutes
submitted through the sub_mpn/sub_manufacturer field arrays.
(Moved from test_part_header.py when the part header endpoints were removed
with the split-panel workspace — simplification spec §5.1.)

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.models import Requirement, Requisition
from tests.conftest import engine  # noqa: F401


def _make_requisition(db, user_id, name="REQ-HDR-001", customer_name="Acme Corp"):
    """Helper to create a requisition."""
    req = Requisition(
        name=name,
        customer_name=customer_name,
        status="open",
        created_by=user_id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    return req


def _make_requirement(db, requisition_id, **kwargs):
    """Helper to create a requirement with optional overrides."""
    defaults = {
        "requisition_id": requisition_id,
        "primary_mpn": "LM317T",
        "manufacturer": "Texas Instruments",
        "brand": "Texas Instruments",
        "target_qty": 5000,
        "target_price": Decimal("1.2500"),
        "condition": "New",
        "sourcing_status": "sourcing",
        "created_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    item = Requirement(**defaults)
    db.add(item)
    db.flush()
    return item


def test_add_requirement_with_substitutes(client, db_session, test_user):
    """POST add requirement saves substitutes via sub_mpn/sub_manufacturer fields."""
    requisition = _make_requisition(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/requisitions/{requisition.id}/requirements",
        data={
            "primary_mpn": "STM32F407VG",
            "manufacturer": "STMicro",
            "target_qty": "100",
            "brand": "ST",
            "sub_mpn": ["STM32F407VI", "STM32F407ZG"],
            "sub_manufacturer": ["STMicro", "STMicro"],
        },
    )
    assert resp.status_code == 200
    html = resp.text
    assert "STM32F407VG" in html
    # Substitutes are now rendered inline instead of "+2 subs" badge
    assert "STM32F407VI" in html
    assert "STM32F407ZG" in html

    # Verify DB
    part = db_session.query(Requirement).filter(Requirement.requisition_id == requisition.id).first()
    assert len(part.substitutes) == 2


def test_add_requirement_without_substitutes(client, db_session, test_user):
    """POST add requirement works fine without substitutes."""
    requisition = _make_requisition(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/requisitions/{requisition.id}/requirements",
        data={"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": "50"},
    )
    assert resp.status_code == 200
    part = db_session.query(Requirement).filter(Requirement.requisition_id == requisition.id).first()
    assert part.substitutes == []


def test_update_requirement_with_substitutes(client, db_session, test_user):
    """PUT update requirement saves substitutes via sub_mpn/sub_manufacturer fields."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, primary_mpn="LM317T")
    db_session.commit()

    resp = client.put(
        f"/v2/partials/requisitions/{requisition.id}/requirements/{part.id}",
        data={
            "primary_mpn": "LM317T",
            "manufacturer": "TI",
            "target_qty": "100",
            "sub_mpn": ["LM317AHVT", "LM317MDT"],
            "sub_manufacturer": ["TI", "TI"],
        },
    )
    assert resp.status_code == 200
    db_session.refresh(part)
    assert len(part.substitutes) == 2
    mpns = [s["mpn"] for s in part.substitutes]
    assert "LM317AHVT" in mpns

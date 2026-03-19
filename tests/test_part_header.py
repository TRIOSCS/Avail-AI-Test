"""test_part_header.py — Tests for the part detail header endpoint.

Tests the GET /v2/partials/parts/{id}/header display endpoint that renders
the persistent context strip above the tab panel.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.models import Requirement, Requisition
from tests.conftest import engine  # noqa: F401


def _make_requisition(db, user_id, name="REQ-HDR-001", customer_name="Acme Corp"):
    """Helper to create a requisition."""
    req = Requisition(
        name=name,
        customer_name=customer_name,
        status="active",
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_requirement(db, requisition_id, **kwargs):
    """Helper to create a requirement with optional overrides."""
    defaults = {
        "requisition_id": requisition_id,
        "primary_mpn": "LM317T",
        "brand": "Texas Instruments",
        "target_qty": 5000,
        "target_price": Decimal("1.2500"),
        "condition": "New",
        "sourcing_status": "sourcing",
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(kwargs)
    item = Requirement(**defaults)
    db.add(item)
    db.flush()
    return item


def test_part_header_returns_200(client, db_session, test_user):
    """GET /v2/partials/parts/{id}/header returns 200 with part data."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/header")
    assert resp.status_code == 200
    html = resp.text
    assert "LM317T" in html
    assert "Texas Instruments" in html
    assert "Sourcing" in html
    assert "5,000" in html
    assert "$1.2500" in html
    assert "New" in html
    assert "REQ-HDR-001" in html
    assert "Acme Corp" in html


def test_part_header_missing_part_returns_404(client, db_session, test_user):
    """GET /v2/partials/parts/99999/header returns 404."""
    resp = client.get("/v2/partials/parts/99999/header")
    assert resp.status_code == 404


def test_part_header_null_fields(client, db_session, test_user):
    """Header renders gracefully when optional fields are null."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(
        db_session,
        requisition.id,
        primary_mpn="UNKNOWN",
        brand=None,
        target_qty=None,
        target_price=None,
        condition=None,
        sourcing_status=None,
    )
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/header")
    assert resp.status_code == 200
    html = resp.text
    assert "UNKNOWN" in html
    assert "No brand" in html
    assert "Open" in html  # default when sourcing_status is None


def test_edit_cell_returns_input(client, db_session, test_user):
    """GET edit/{field} returns an input or select element."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/header/edit/brand")
    assert resp.status_code == 200
    assert "input" in resp.text.lower() or "select" in resp.text.lower()


def test_edit_cell_invalid_field(client, db_session, test_user):
    """GET edit/bogus returns 400."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/header/edit/bogus_field")
    assert resp.status_code == 400


def test_patch_header_updates_field(client, db_session, test_user):
    """PATCH saves target_qty and returns updated header."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/header",
        data={"field": "target_qty", "value": "5000"},
    )
    assert resp.status_code == 200
    db_session.refresh(part)
    assert part.target_qty == 5000


def test_patch_header_hx_trigger(client, db_session, test_user):
    """PATCH response includes HX-Trigger for list sync."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/header",
        data={"field": "brand", "value": "TI"},
    )
    assert resp.status_code == 200
    assert "part-updated" in resp.headers.get("hx-trigger", "")

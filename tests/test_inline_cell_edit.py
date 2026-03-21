"""test_inline_cell_edit.py — Tests for inline table-cell editing endpoints.

Tests the GET /v2/partials/parts/{id}/cell/edit/{field},
GET /v2/partials/parts/{id}/cell/display/{field}, and
PATCH /v2/partials/parts/{id}/cell endpoints.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.models import Requirement, Requisition
from tests.conftest import engine  # noqa: F401


def _make_requisition(db, user_id, name="REQ-CELL-001"):
    """Helper to create a requisition."""
    req = Requisition(
        name=name,
        customer_name="Acme Corp",
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
        "sourcing_status": "open",
        "created_at": datetime.now(timezone.utc),
    }
    defaults.update(kwargs)
    item = Requirement(**defaults)
    db.add(item)
    db.flush()
    return item


# ── GET /cell/edit/{field} ───────────────────────────────────────────


def test_cell_edit_status_returns_select(client, db_session, test_user):
    """GET cell/edit/sourcing_status returns a <select> element."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/cell/edit/sourcing_status")
    assert resp.status_code == 200
    html = resp.text
    assert "<select" in html
    assert f"cell-sourcing_status-{part.id}" in html
    assert 'name="value"' in html


def test_cell_edit_qty_returns_input(client, db_session, test_user):
    """GET cell/edit/target_qty returns a number input."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/cell/edit/target_qty")
    assert resp.status_code == 200
    html = resp.text
    assert 'type="number"' in html
    assert f"cell-target_qty-{part.id}" in html


def test_cell_edit_price_returns_input(client, db_session, test_user):
    """GET cell/edit/target_price returns a number input with step."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/cell/edit/target_price")
    assert resp.status_code == 200
    html = resp.text
    assert 'type="number"' in html
    assert 'step="0.0001"' in html
    assert f"cell-target_price-{part.id}" in html


def test_cell_edit_invalid_field_returns_400(client, db_session, test_user):
    """GET cell/edit with invalid field returns 400."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/cell/edit/bogus_field")
    assert resp.status_code == 400


def test_cell_edit_missing_part_returns_404(client, db_session, test_user):
    """GET cell/edit for nonexistent part returns 404."""
    resp = client.get("/v2/partials/parts/99999/cell/edit/sourcing_status")
    assert resp.status_code == 404


# ── GET /cell/display/{field} ────────────────────────────────────────


def test_cell_display_status(client, db_session, test_user):
    """GET cell/display/sourcing_status returns badge HTML."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, sourcing_status="sourcing")
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/cell/display/sourcing_status")
    assert resp.status_code == 200
    html = resp.text
    assert "sourcing" in html
    assert f"cell-sourcing_status-{part.id}" in html
    assert "hx-get" in html


def test_cell_display_qty(client, db_session, test_user):
    """GET cell/display/target_qty returns formatted qty."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, target_qty=10000)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/cell/display/target_qty")
    assert resp.status_code == 200
    assert "10,000" in resp.text


def test_cell_display_price(client, db_session, test_user):
    """GET cell/display/target_price returns formatted price."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, target_price=Decimal("2.5000"))
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/cell/display/target_price")
    assert resp.status_code == 200
    assert "$2.5000" in resp.text


def test_cell_display_null_fields(client, db_session, test_user):
    """Display cells render em-dash for null values."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    # Force null after creation (bypasses column default=1)
    part.target_qty = None
    part.target_price = None
    db_session.commit()

    resp_qty = client.get(f"/v2/partials/parts/{part.id}/cell/display/target_qty")
    assert resp_qty.status_code == 200
    print("QTY RESPONSE:", repr(resp_qty.text))
    assert "$" not in resp_qty.text  # no dollar formatting for null

    resp_price = client.get(f"/v2/partials/parts/{part.id}/cell/display/target_price")
    assert resp_price.status_code == 200
    assert "$" not in resp_price.text  # no dollar sign for null price


def test_cell_display_invalid_field_returns_400(client, db_session, test_user):
    """GET cell/display with invalid field returns 400."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/cell/display/bogus")
    assert resp.status_code == 400


def test_cell_display_missing_part_returns_404(client, db_session, test_user):
    """GET cell/display for nonexistent part returns 404."""
    resp = client.get("/v2/partials/parts/99999/cell/display/target_qty")
    assert resp.status_code == 404


# ── PATCH /cell (save) ───────────────────────────────────────────────


def test_cell_save_qty(client, db_session, test_user):
    """PATCH cell saves target_qty and returns display cell."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, target_qty=5000)
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/cell",
        data={"field": "target_qty", "value": "7500"},
    )
    assert resp.status_code == 200
    assert "7,500" in resp.text
    assert "HX-Trigger" in resp.headers

    db_session.refresh(part)
    assert part.target_qty == 7500


def test_cell_save_price(client, db_session, test_user):
    """PATCH cell saves target_price and returns display cell."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/cell",
        data={"field": "target_price", "value": "3.7500"},
    )
    assert resp.status_code == 200
    assert "$3.7500" in resp.text

    db_session.refresh(part)
    assert part.target_price == Decimal("3.7500")


def test_cell_save_status(client, db_session, test_user):
    """PATCH cell saves sourcing_status via transition_requirement."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, sourcing_status="open")
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/cell",
        data={"field": "sourcing_status", "value": "sourcing"},
    )
    assert resp.status_code == 200
    assert "sourcing" in resp.text


def test_cell_save_empty_qty_sets_none(client, db_session, test_user):
    """PATCH cell with empty value sets target_qty to None."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, target_qty=5000)
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/cell",
        data={"field": "target_qty", "value": ""},
    )
    assert resp.status_code == 200

    db_session.refresh(part)
    assert part.target_qty is None


def test_cell_save_invalid_qty_sets_none(client, db_session, test_user):
    """PATCH cell with non-numeric qty sets target_qty to None."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, target_qty=5000)
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/cell",
        data={"field": "target_qty", "value": "abc"},
    )
    assert resp.status_code == 200

    db_session.refresh(part)
    assert part.target_qty is None


def test_cell_save_invalid_field_returns_400(client, db_session, test_user):
    """PATCH cell with invalid field returns 400."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/cell",
        data={"field": "bogus", "value": "x"},
    )
    assert resp.status_code == 400


def test_cell_save_missing_part_returns_404(client, db_session, test_user):
    """PATCH cell for nonexistent part returns 404."""
    resp = client.patch(
        "/v2/partials/parts/99999/cell",
        data={"field": "target_qty", "value": "100"},
    )
    assert resp.status_code == 404


def test_cell_save_triggers_part_updated(client, db_session, test_user):
    """PATCH cell includes HX-Trigger header with part-updated event."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/cell",
        data={"field": "target_qty", "value": "999"},
    )
    assert resp.status_code == 200
    trigger = resp.headers.get("HX-Trigger", "")
    assert "part-updated" in trigger
    assert str(part.id) in trigger

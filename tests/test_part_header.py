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


def test_header_shows_substitutes(client, db_session, test_user):
    """Header renders substitute pills when present."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(
        db_session,
        requisition.id,
        substitutes=["LM317AHVT", "LM317MDT"],
    )
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/header")
    assert resp.status_code == 200
    html = resp.text
    assert "LM317AHVT" in html
    assert "LM317MDT" in html
    assert "Subs:" in html


def test_header_no_substitutes_shows_add_link(client, db_session, test_user):
    """Header shows '+ Add substitutes' when none exist."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, substitutes=[])
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/header")
    assert resp.status_code == 200
    assert "Add substitutes" in resp.text


def test_edit_substitutes_returns_input(client, db_session, test_user):
    """GET edit/substitutes returns comma-separated input."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(
        db_session,
        requisition.id,
        substitutes=["LM317AHVT", "LM317MDT"],
    )
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{part.id}/header/edit/substitutes")
    assert resp.status_code == 200
    assert "LM317AHVT" in resp.text
    assert "input" in resp.text.lower()


def test_patch_header_saves_substitutes(client, db_session, test_user):
    """PATCH substitutes saves normalized, deduplicated list."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id)
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/header",
        data={"field": "substitutes", "value": "LM317AHVT, lm317ahvt, LM317MDT"},
    )
    assert resp.status_code == 200
    db_session.refresh(part)
    assert len(part.substitutes) == 2
    assert "LM317AHVT" in part.substitutes
    assert "LM317MDT" in part.substitutes


def test_patch_header_substitutes_excludes_primary(client, db_session, test_user):
    """PATCH substitutes excludes the primary MPN from the list."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, primary_mpn="LM317T")
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/header",
        data={"field": "substitutes", "value": "LM317T, LM317AHVT"},
    )
    assert resp.status_code == 200
    db_session.refresh(part)
    assert "LM317T" not in part.substitutes
    assert "LM317AHVT" in part.substitutes


def test_patch_header_clear_substitutes(client, db_session, test_user):
    """PATCH with empty value clears substitutes."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(
        db_session,
        requisition.id,
        substitutes=["LM317AHVT"],
    )
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/parts/{part.id}/header",
        data={"field": "substitutes", "value": ""},
    )
    assert resp.status_code == 200
    db_session.refresh(part)
    assert part.substitutes == []


# ── Add / Update requirement with substitutes ─────────────────────────


def test_add_requirement_with_substitutes(client, db_session, test_user):
    """POST add requirement saves substitutes."""
    requisition = _make_requisition(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/requisitions/{requisition.id}/requirements",
        data={
            "primary_mpn": "STM32F407VG",
            "target_qty": "100",
            "brand": "ST",
            "substitutes": "STM32F407VI, STM32F407ZG",
        },
    )
    assert resp.status_code == 200
    html = resp.text
    assert "STM32F407VG" in html
    assert "+2 subs" in html

    # Verify DB
    part = db_session.query(Requirement).filter(Requirement.requisition_id == requisition.id).first()
    assert len(part.substitutes) == 2


def test_add_requirement_without_substitutes(client, db_session, test_user):
    """POST add requirement works fine without substitutes."""
    requisition = _make_requisition(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/requisitions/{requisition.id}/requirements",
        data={"primary_mpn": "LM317T", "target_qty": "50"},
    )
    assert resp.status_code == 200
    part = db_session.query(Requirement).filter(Requirement.requisition_id == requisition.id).first()
    assert part.substitutes == []


def test_update_requirement_with_substitutes(client, db_session, test_user):
    """PUT update requirement saves substitutes."""
    requisition = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, requisition.id, primary_mpn="LM317T")
    db_session.commit()

    resp = client.put(
        f"/v2/partials/requisitions/{requisition.id}/requirements/{part.id}",
        data={
            "primary_mpn": "LM317T",
            "target_qty": "100",
            "substitutes": "LM317AHVT, LM317MDT",
        },
    )
    assert resp.status_code == 200
    db_session.refresh(part)
    assert len(part.substitutes) == 2
    assert "LM317AHVT" in part.substitutes

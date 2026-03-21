"""test_htmx_material_card_auto.py — Tests for auto-creating MaterialCards in HTMX
routes.

Verifies that all 4 HTMX paths that create/edit requirements also resolve
and link a MaterialCard via resolve_material_card().

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone

from app.models import Requirement, Requisition
from app.models.intelligence import MaterialCard
from tests.conftest import engine  # noqa: F401


def _make_requisition(db, user_id, name="REQ-MC-001"):
    req = Requisition(
        name=name,
        customer_name="Test Corp",
        status="active",
        created_by=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_requirement(db, requisition_id, primary_mpn="LM317T"):
    r = Requirement(
        requisition_id=requisition_id,
        primary_mpn=primary_mpn,
        target_qty=100,
        sourcing_status="open",
    )
    db.add(r)
    db.flush()
    return r


# ── Path 1: Create requisition with indexed form parts ────────────────


def test_create_requisition_with_parts_links_material_card(client, db_session, test_user):
    """POST /v2/partials/requisitions/import-save creates MaterialCard for each part."""
    resp = client.post(
        "/v2/partials/requisitions/import-save",
        data={
            "name": "REQ-IMPORT-MC",
            "customer_name": "Acme",
            "urgency": "normal",
            "deadline": "",
            "reqs[0].primary_mpn": "STM32F407VG",
            "reqs[0].target_qty": "10",
            "reqs[0].brand": "",
            "reqs[0].target_price": "",
            "reqs[0].condition": "new",
            "reqs[0].customer_pn": "",
        },
    )
    assert resp.status_code == 200

    req_part = db_session.query(Requirement).filter(Requirement.primary_mpn == "STM32F407VG").first()
    assert req_part is not None
    assert req_part.material_card_id is not None

    card = db_session.get(MaterialCard, req_part.material_card_id)
    assert card is not None
    assert "stm32f407vg" in card.normalized_mpn


# ── Path 2: Quick-add from text ───────────────────────────────────────


def test_quick_add_text_links_material_card(client, db_session, test_user):
    """POST /v2/partials/requisitions/create with parts_text creates MaterialCards."""
    resp = client.post(
        "/v2/partials/requisitions/create",
        data={
            "name": "REQ-QUICK",
            "customer_name": "Quick Corp",
            "parts_text": "AD7124-8BCPZ 50\nLTC2983HLX 25",
        },
    )
    assert resp.status_code == 200

    parts = db_session.query(Requirement).join(Requisition).filter(Requisition.name == "REQ-QUICK").all()
    assert len(parts) == 2

    for part in parts:
        assert part.material_card_id is not None, f"No material_card_id for {part.primary_mpn}"
        assert part.normalized_mpn is not None, f"No normalized_mpn for {part.primary_mpn}"


# ── Path 3: Add single requirement ───────────────────────────────────


def test_add_single_requirement_links_material_card(client, db_session, test_user):
    """POST add requirement resolves and links MaterialCard."""
    req = _make_requisition(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/requisitions/{req.id}/requirements",
        data={"primary_mpn": "MAX17498AATE", "target_qty": "100"},
    )
    assert resp.status_code == 200

    part = db_session.query(Requirement).filter(Requirement.requisition_id == req.id).first()
    assert part.material_card_id is not None

    card = db_session.get(MaterialCard, part.material_card_id)
    assert card is not None
    assert "max17498aate" in card.normalized_mpn


# ── Path 4: Inline edit updates material card ─────────────────────────


def test_inline_edit_links_material_card(client, db_session, test_user):
    """PUT inline edit resolves MaterialCard when MPN changes."""
    req = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, req.id, primary_mpn="OLD-MPN-123")
    db_session.commit()

    resp = client.put(
        f"/v2/partials/requisitions/{req.id}/requirements/{part.id}",
        data={"primary_mpn": "TPS65381AQPHPRQ1", "target_qty": "200"},
    )
    assert resp.status_code == 200

    db_session.refresh(part)
    assert part.material_card_id is not None
    assert part.normalized_mpn is not None

    card = db_session.get(MaterialCard, part.material_card_id)
    assert card is not None
    assert "tps65381aqphprq1" in card.normalized_mpn


def test_inline_edit_reuses_existing_material_card(client, db_session, test_user):
    """PUT inline edit reuses an existing MaterialCard for the same MPN."""
    # Pre-create a material card
    existing = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=0)
    db_session.add(existing)
    db_session.flush()

    req = _make_requisition(db_session, test_user.id)
    part = _make_requirement(db_session, req.id, primary_mpn="OLD-PART")
    db_session.commit()

    resp = client.put(
        f"/v2/partials/requisitions/{req.id}/requirements/{part.id}",
        data={"primary_mpn": "LM317T", "target_qty": "50"},
    )
    assert resp.status_code == 200

    db_session.refresh(part)
    assert part.material_card_id == existing.id  # Reused, not duplicated

"""test_phase8_materials.py — Tests for Phase 8: Material Management.

Verifies: material card list (search, lifecycle filter, pagination),
detail view (specs, sightings, offers), update card fields.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def material_cards(db_session: Session):
    """Create a few material cards for testing."""
    from app.models.intelligence import MaterialCard

    cards = []
    for mpn, mfr, lifecycle, category, searches in [
        ("LM317T", "TI", "active", "Voltage Regulator", 42),
        ("NE555P", "TI", "active", "Timer", 30),
        ("STM32F103", "STMicro", "eol", "Microcontroller", 15),
        ("MAX232", "Maxim", "obsolete", "Interface", 5),
    ]:
        card = MaterialCard(
            normalized_mpn=mpn.lower(),
            display_mpn=mpn,
            manufacturer=mfr,
            lifecycle_status=lifecycle,
            category=category,
            search_count=searches,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        cards.append(card)

    db_session.commit()
    for c in cards:
        db_session.refresh(c)
    return cards


@pytest.fixture()
def card_with_sightings(db_session: Session, material_cards, test_user: User):
    """The first material card (LM317T) with sightings."""
    from app.models import Requirement, Requisition, Sighting

    card = material_cards[0]  # LM317T

    # Create a requisition and requirement for the sighting FK
    req = Requisition(
        name="Sighting Test Req",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    requirement = Requirement(requisition_id=req.id, primary_mpn="LM317T", target_qty=100)
    db_session.add(requirement)
    db_session.flush()

    for vendor, price, qty in [
        ("Arrow", 0.45, 5000),
        ("Mouser", 0.50, 10000),
        ("DigiKey", 0.48, 7500),
    ]:
        s = Sighting(
            requirement_id=requirement.id,
            material_card_id=card.id,
            mpn_matched="LM317T",
            vendor_name=vendor,
            unit_price=price,
            qty_available=qty,
            source_type="api",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)

    db_session.commit()
    return card


# ── Material List ────────────────────────────────────────────────────


class TestMaterialList:
    """Tests for the material cards list view."""

    def test_list_loads(self, client: TestClient, material_cards):
        resp = client.get("/v2/partials/materials")
        assert resp.status_code == 200
        assert "Materials" in resp.text
        assert "LM317T" in resp.text

    def test_list_shows_all_cards(self, client: TestClient, material_cards):
        resp = client.get("/v2/partials/materials")
        assert resp.status_code == 200
        assert "NE555P" in resp.text
        assert "STM32F103" in resp.text
        assert "MAX232" in resp.text

    def test_search_by_mpn(self, client: TestClient, material_cards):
        resp = client.get("/v2/partials/materials?q=LM317")
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert "NE555P" not in resp.text

    def test_filter_by_lifecycle(self, client: TestClient, material_cards):
        resp = client.get("/v2/partials/materials?lifecycle=obsolete")
        assert resp.status_code == 200
        assert "MAX232" in resp.text
        assert "LM317T" not in resp.text

    def test_filter_eol(self, client: TestClient, material_cards):
        resp = client.get("/v2/partials/materials?lifecycle=eol")
        assert resp.status_code == 200
        assert "STM32F103" in resp.text

    def test_empty_search(self, client: TestClient, material_cards):
        resp = client.get("/v2/partials/materials?q=NONEXISTENT")
        assert resp.status_code == 200
        assert "No material cards found" in resp.text

    def test_empty_db(self, client: TestClient):
        resp = client.get("/v2/partials/materials")
        assert resp.status_code == 200
        assert "No material cards found" in resp.text


# ── Material Detail ──────────────────────────────────────────────────


class TestMaterialDetail:
    """Tests for the material card detail view."""

    def test_detail_loads(self, client: TestClient, material_cards):
        card = material_cards[0]  # LM317T
        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert "TI" in resp.text
        assert "Voltage Regulator" in resp.text

    def test_detail_shows_lifecycle_badge(
        self, client: TestClient, material_cards
    ):
        card = material_cards[2]  # STM32F103 - EOL
        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert "EOL" in resp.text

    def test_detail_shows_sightings(
        self, client: TestClient, card_with_sightings
    ):
        resp = client.get(f"/v2/partials/materials/{card_with_sightings.id}")
        assert resp.status_code == 200
        assert "Arrow" in resp.text
        assert "Mouser" in resp.text
        assert "Recent Sightings" in resp.text

    def test_detail_404(self, client: TestClient):
        resp = client.get("/v2/partials/materials/99999")
        assert resp.status_code == 404

    def test_detail_has_edit_form(self, client: TestClient, material_cards):
        card = material_cards[0]
        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert "Edit" in resp.text
        assert "Specifications" in resp.text


# ── Material Update ──────────────────────────────────────────────────


class TestMaterialUpdate:
    """Tests for updating material card fields."""

    def test_update_manufacturer(
        self, client: TestClient, db_session: Session, material_cards
    ):
        card = material_cards[0]  # LM317T
        resp = client.put(
            f"/v2/partials/materials/{card.id}",
            data={"manufacturer": "Texas Instruments", "category": "Linear Regulator"},
        )
        assert resp.status_code == 200
        assert "Texas Instruments" in resp.text

        db_session.refresh(card)
        assert card.manufacturer == "Texas Instruments"
        assert card.category == "Linear Regulator"

    def test_update_lifecycle(
        self, client: TestClient, db_session: Session, material_cards
    ):
        card = material_cards[0]
        resp = client.put(
            f"/v2/partials/materials/{card.id}",
            data={"lifecycle_status": "eol"},
        )
        assert resp.status_code == 200

        db_session.refresh(card)
        assert card.lifecycle_status == "eol"

    def test_update_404(self, client: TestClient):
        resp = client.put(
            "/v2/partials/materials/99999",
            data={"manufacturer": "Test"},
        )
        assert resp.status_code == 404

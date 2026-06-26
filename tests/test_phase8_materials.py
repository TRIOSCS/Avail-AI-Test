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
    # Canonical commodity keys (the @validates guard rejects off-vocab); the detail
    # template renders card.category verbatim, so assertions match these exact strings.
    for mpn, mfr, lifecycle, category, searches in [
        ("LM317T", "TI", "active", "voltage_regulators", 42),
        ("NE555P", "TI", "active", "ics_other", 30),
        ("STM32F103", "STMicro", "eol", "microcontrollers", 15),
        ("MAX232", "Maxim", "obsolete", "ics_other", 5),
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
        status="open",
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
        assert "materials-workspace" in resp.text

    def test_list_shows_workspace_structure(self, client: TestClient, material_cards):
        resp = client.get("/v2/partials/materials")
        assert resp.status_code == 200
        assert "materialsFilter" in resp.text
        assert "Category" in resp.text

    @pytest.mark.parametrize(
        "query, expected_substring",
        [
            pytest.param("LM317", "LM317T", id="search_by_mpn"),
            pytest.param("obsolete", None, id="filter_by_lifecycle"),
            pytest.param("eol", None, id="filter_eol"),
            pytest.param("NONEXISTENT999", None, id="empty_search"),
        ],
    )
    def test_faceted_search(self, client: TestClient, material_cards, query, expected_substring):
        """Faceted search results endpoint responds 200 (and returns matching cards)."""
        resp = client.get(f"/v2/partials/materials/faceted?q={query}")
        assert resp.status_code == 200
        if expected_substring is not None:
            assert expected_substring in resp.text

    def test_empty_db(self, client: TestClient):
        """Workspace loads even with no material cards."""
        resp = client.get("/v2/partials/materials")
        assert resp.status_code == 200
        assert "materials-workspace" in resp.text


# ── Material Detail ──────────────────────────────────────────────────


class TestMaterialDetail:
    """Tests for the material card detail view."""

    def test_detail_loads(self, client: TestClient, material_cards):
        card = material_cards[0]  # LM317T
        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert "LM317T" in resp.text
        assert "TI" in resp.text
        assert "voltage_regulators" in resp.text

    def test_detail_shows_lifecycle_badge(self, client: TestClient, material_cards):
        card = material_cards[2]  # STM32F103 - EOL
        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert "EOL" in resp.text

    def test_detail_has_tab_structure(self, client: TestClient, card_with_sightings):
        """Detail page should show tab bar (sightings moved to lazy-loaded tabs)."""
        resp = client.get(f"/v2/partials/materials/{card_with_sightings.id}")
        assert resp.status_code == 200
        assert "material-tab-content" in resp.text
        assert "Vendors" in resp.text
        assert "Price History" in resp.text

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

    def test_update_manufacturer(self, client: TestClient, db_session: Session, material_cards):
        card = material_cards[0]  # LM317T, seeded category "voltage_regulators"
        # Post a DIFFERENT canonical category so the ladder records a genuine manual edit
        # (the route no-ops an unchanged value rather than re-stamping it as manual).
        resp = client.put(
            f"/v2/partials/materials/{card.id}",
            data={"manufacturer": "Texas Instruments", "category": "analog_ic"},
        )
        assert resp.status_code == 200
        assert "Texas Instruments" in resp.text

        db_session.refresh(card)
        assert card.manufacturer == "Texas Instruments"
        # Category routes through the F1 ladder (set_category) — it lands canonical with
        # manual provenance; off-vocab free text like "Linear Regulator" would be rejected.
        assert card.category == "analog_ic"
        assert card.category_source == "manual"

    def test_update_lifecycle(self, client: TestClient, db_session: Session, material_cards):
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

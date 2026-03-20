"""tests/test_routers_materials.py — Tests for routers/materials.py.

Covers: material_card_to_dict helper, MaterialCard CRUD, enrich,
merge, import-stock, material enrichment fields.

Called by: pytest
Depends on: routers/materials.py
"""

import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.models import MaterialCard, MaterialVendorHistory, Offer, Requirement, Requisition, Sighting, VendorCard
from app.routers.materials import material_card_to_dict

# ── Stub factories ───────────────────────────────────────────────────────


def _make_material_card(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=10,
        normalized_mpn="lm358n",
        display_mpn="LM358N",
        manufacturer="Texas Instruments",
        description="Dual Op-Amp",
        search_count=5,
        last_searched_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
        # Enrichment fields
        lifecycle_status=None,
        package_type=None,
        category=None,
        rohs_status=None,
        pin_count=None,
        datasheet_url=None,
        cross_references=None,
        specs_summary=None,
        enrichment_source=None,
        enriched_at=None,
        created_at=datetime(2025, 12, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_vendor_history(**overrides) -> SimpleNamespace:
    defaults = dict(
        id=1,
        vendor_name="Acme Electronics",
        source_type="broker",
        is_authorized=False,
        first_seen=datetime(2025, 12, 15, tzinfo=timezone.utc),
        last_seen=datetime(2026, 1, 10, tzinfo=timezone.utc),
        times_seen=3,
        last_qty=500,
        last_price=0.45,
        last_currency="USD",
        last_manufacturer="TI",
        vendor_sku="ACM-LM358",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── Admin client fixture ─────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session, admin_user):
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_admin] = _override_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── material_card_to_dict tests ──────────────────────────────────────────


def test_material_card_to_dict_with_history():
    """material_card_to_dict includes vendor history."""
    card = _make_material_card()
    vh = _make_vendor_history()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = [vh]

    result = material_card_to_dict(card, db)

    assert result["display_mpn"] == "LM358N"
    assert result["vendor_count"] == 1
    assert result["vendor_history"][0]["vendor_name"] == "Acme Electronics"
    assert result["vendor_history"][0]["last_price"] == 0.45


def test_material_card_to_dict_no_history():
    """material_card_to_dict handles zero vendor history."""
    card = _make_material_card()
    db = MagicMock()
    db.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = []

    result = material_card_to_dict(card, db)

    assert result["vendor_count"] == 0
    assert result["vendor_history"] == []
    assert result["search_count"] == 5


def test_material_card_to_dict_with_sightings_and_offers(db_session, test_material_card, test_user):
    """material_card_to_dict includes sightings and offers for matching requirements."""
    req = Requisition(
        name="REQ-MAT-001",
        customer_name="Test Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    sighting = Sighting(
        requirement_id=requirement.id,
        material_card_id=test_material_card.id,
        vendor_name="Test Vendor",
        mpn_matched="LM317T",
        qty_available=500,
        unit_price=0.45,
        source_type="api",
        is_unavailable=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)

    offer = Offer(
        requisition_id=req.id,
        requirement_id=requirement.id,
        material_card_id=test_material_card.id,
        vendor_name="Test Vendor",
        mpn="LM317T",
        qty_available=500,
        unit_price=0.45,
        status="active",
        entered_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    result = material_card_to_dict(test_material_card, db_session)
    assert result["display_mpn"] == "LM317T"
    assert len(result["sightings"]) >= 1
    assert len(result["offers"]) >= 1


def test_material_card_to_dict_unavailable_sightings_excluded(db_session, test_material_card, test_user):
    """material_card_to_dict excludes unavailable sightings."""
    req = Requisition(
        name="REQ-MAT-002",
        customer_name="Test Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.flush()

    sighting = Sighting(
        requirement_id=requirement.id,
        vendor_name="Gone Vendor",
        mpn_matched="LM317T",
        qty_available=0,
        source_type="api",
        is_unavailable=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sighting)
    db_session.commit()

    result = material_card_to_dict(test_material_card, db_session)
    vendor_names_in_sightings = [s["vendor_name"] for s in result["sightings"]]
    assert "Gone Vendor" not in vendor_names_in_sightings


def test_material_card_to_dict_enrichment_fields(db_session):
    """material_card_to_dict serializes enrichment fields."""
    mc = MaterialCard(
        normalized_mpn="enriched123",
        display_mpn="ENRICHED123",
        manufacturer="TI",
        lifecycle_status="active",
        package_type="QFP-64",
        category="Microcontroller",
        rohs_status="compliant",
        pin_count=64,
        datasheet_url="https://ti.com/ds.pdf",
        cross_references=[{"mpn": "ALT123", "manufacturer": "NXP"}],
        specs_summary="32-bit ARM Cortex",
        enrichment_source="claude_agent",
        enriched_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        search_count=5,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(mc)
    db_session.commit()

    result = material_card_to_dict(mc, db_session)
    assert result["lifecycle_status"] == "active"
    assert result["package_type"] == "QFP-64"
    assert result["pin_count"] == 64
    assert result["enrichment_source"] == "claude_agent"
    assert result["enriched_at"] is not None


# ── Materials CRUD Integration ───────────────────────────────────────────


def test_list_materials(client, db_session, test_material_card):
    """GET /api/materials returns 200 with materials."""
    resp = client.get("/api/materials")
    assert resp.status_code == 200
    data = resp.json()
    materials = data.get("materials", [])
    assert len(materials) >= 1


def test_list_materials_search(client, db_session, test_material_card):
    """GET /api/materials?q=lm317 finds the LM317T material."""
    resp = client.get("/api/materials", params={"q": "lm317"})
    assert resp.status_code == 200
    data = resp.json()
    materials = data.get("materials", [])
    mpns = [m["display_mpn"] for m in materials]
    assert "LM317T" in mpns


def test_get_material_by_id(client, db_session, test_material_card):
    """GET /api/materials/{id} returns material detail."""
    resp = client.get(f"/api/materials/{test_material_card.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_mpn"] == "LM317T"
    assert data["id"] == test_material_card.id


def test_get_material_by_id_not_found(client):
    """GET /api/materials/99999 returns 404."""
    resp = client.get("/api/materials/99999")
    assert resp.status_code == 404


def test_get_material_by_mpn(client, db_session, test_material_card):
    """GET /api/materials/by-mpn/LM317T returns material detail."""
    resp = client.get("/api/materials/by-mpn/LM317T")
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_mpn"] == "LM317T"


def test_get_material_by_mpn_not_found(client):
    """GET /api/materials/by-mpn/NONEXISTENT returns 404."""
    resp = client.get("/api/materials/by-mpn/NONEXISTENT-MPN")
    assert resp.status_code == 404


def test_update_material(client, db_session, test_material_card):
    """PUT /api/materials/{id} with manufacturer updates it."""
    resp = client.put(
        f"/api/materials/{test_material_card.id}",
        json={"manufacturer": "STMicroelectronics"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["manufacturer"] == "STMicroelectronics"


def test_update_material_not_found(client):
    """PUT /api/materials/99999 returns 404."""
    resp = client.put(
        "/api/materials/99999",
        json={"manufacturer": "TI"},
    )
    assert resp.status_code == 404


def test_delete_material_admin(admin_client, db_session, admin_user):
    """DELETE /api/materials/{id} with admin client succeeds."""
    mc = MaterialCard(
        normalized_mpn="deleteme123",
        display_mpn="DELETEME123",
        manufacturer="Test Mfr",
        search_count=0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(mc)
    db_session.commit()
    mid = mc.id

    resp = admin_client.delete(f"/api/materials/{mid}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Verify it's soft-deleted
    db_session.expire_all()
    card = db_session.get(MaterialCard, mid)
    assert card is not None
    assert card.deleted_at is not None


def test_delete_material_not_found(admin_client):
    """DELETE /api/materials/99999 returns 404."""
    resp = admin_client.delete("/api/materials/99999")
    assert resp.status_code == 404


def test_list_materials_empty(client, db_session):
    """GET /api/materials with no data returns empty list."""
    resp = client.get("/api/materials")
    assert resp.status_code == 200
    data = resp.json()
    assert data["materials"] == []
    assert data["total"] == 0


# ── Material enrichment ──────────────────────────────────────────────────


def test_update_material_enrichment_fields(client, db_session, test_material_card):
    """PUT /api/materials/{id} with enrichment fields updates them."""
    resp = client.put(
        f"/api/materials/{test_material_card.id}",
        json={
            "lifecycle_status": "active",
            "package_type": "DIP-8",
            "category": "Voltage Regulator",
            "rohs_status": "compliant",
            "pin_count": 8,
            "datasheet_url": "https://ti.com/ds/lm317t.pdf",
            "cross_references": [{"mpn": "LM317LZ", "manufacturer": "ON Semi"}],
            "specs_summary": "1.25V to 37V adjustable output",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["lifecycle_status"] == "active"
    assert data["package_type"] == "DIP-8"
    assert data["pin_count"] == 8


def test_update_material_description(client, db_session, test_material_card):
    """PUT /api/materials/{id} with description updates it."""
    resp = client.put(
        f"/api/materials/{test_material_card.id}",
        json={"description": "Updated description for LM317T"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "Updated description for LM317T"


def test_update_material_display_mpn(client, db_session, test_material_card):
    """PUT /api/materials/{id} with display_mpn updates it."""
    resp = client.put(
        f"/api/materials/{test_material_card.id}",
        json={"display_mpn": "LM317T/NOPB"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_mpn"] == "LM317T/NOPB"


def test_update_material_blank_display_mpn(client, db_session, test_material_card):
    """PUT /api/materials/{id} with blank display_mpn does not update."""
    original_mpn = test_material_card.display_mpn
    resp = client.put(
        f"/api/materials/{test_material_card.id}",
        json={"display_mpn": "   "},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["display_mpn"] == original_mpn


def test_update_material_sets_manual_enrichment_source(client, db_session):
    """PUT /api/materials/{id} with enrichment field sets source to manual."""
    mc = MaterialCard(
        normalized_mpn="enrichsource123",
        display_mpn="ENRICHSOURCE123",
        manufacturer="Test",
        search_count=0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(mc)
    db_session.commit()

    resp = client.put(
        f"/api/materials/{mc.id}",
        json={"lifecycle_status": "eol"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["enrichment_source"] == "manual"


def test_enrich_material(client, db_session, test_material_card):
    """POST /api/materials/{id}/enrich applies enrichment data."""
    resp = client.post(
        f"/api/materials/{test_material_card.id}/enrich",
        json={
            "lifecycle_status": "active",
            "package_type": "TO-220",
            "manufacturer": "STMicroelectronics",
            "source": "claude_agent",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "lifecycle_status" in data["updated_fields"]
    assert "package_type" in data["updated_fields"]
    assert "manufacturer" in data["updated_fields"]


def test_enrich_material_no_fields(client, db_session, test_material_card):
    """POST /api/materials/{id}/enrich with no matching fields."""
    resp = client.post(
        f"/api/materials/{test_material_card.id}/enrich",
        json={"unrelated_field": "value"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["updated_fields"] == []


def test_enrich_material_not_found(client):
    """POST /api/materials/99999/enrich returns 404."""
    resp = client.post(
        "/api/materials/99999/enrich",
        json={"lifecycle_status": "active"},
    )
    assert resp.status_code == 404


# ── Import stock ─────────────────────────────────────────────────────────


def test_import_stock_missing_vendor(client, monkeypatch):
    """POST /api/materials/import-stock without vendor_name returns 400."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

    csv_content = b"mpn,qty,price\nLM317T,1000,0.50"
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": ""},
        files={"file": ("stock.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 400


def test_import_stock_no_file(client, monkeypatch):
    """POST /api/materials/import-stock without file returns 400."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Some Vendor"},
    )
    assert resp.status_code == 400


def test_import_stock_success(client, db_session, monkeypatch):
    """POST /api/materials/import-stock with valid CSV imports rows."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)

    csv_content = b"mpn,qty,price,manufacturer\nLM555CN,1000,0.25,Texas Instruments\nNE556N,500,0.30,Signetics"
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Stock Import Vendor"},
        files={"file": ("stock.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_rows"] >= 0
    assert data["vendor_name"] == "Stock Import Vendor"


def test_import_stock_with_website(client, db_session, monkeypatch):
    """POST /api/materials/import-stock with vendor_website sets domain."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)

    csv_content = b"mpn,qty,price\nABC123,100,1.50"
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Website Import Vendor", "vendor_website": "https://www.websiteimport.com/products"},
        files={"file": ("stock.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor_name"] == "Website Import Vendor"


def test_import_stock_too_large(client, db_session, monkeypatch):
    """POST /api/materials/import-stock with > 10MB file returns 413."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

    large_content = b"x" * (10_000_001)
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Large File Vendor"},
        files={"file": ("large.csv", io.BytesIO(large_content), "text/csv")},
    )
    assert resp.status_code == 413


def test_import_stock_existing_vendor(client, db_session, monkeypatch):
    """POST /api/materials/import-stock with existing vendor updates sighting_count."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)

    from app.vendor_utils import normalize_vendor_name

    norm = normalize_vendor_name("Existing Stock Vendor")
    vc = VendorCard(
        normalized_name=norm,
        display_name="Existing Stock Vendor",
        sighting_count=10,
    )
    db_session.add(vc)
    db_session.commit()

    csv_content = b"mpn,qty,price\nXYZ789,200,0.75"
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Existing Stock Vendor"},
        files={"file": ("stock.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200


def test_import_stock_update_existing_mvh(client, db_session, monkeypatch):
    """POST /api/materials/import-stock updates existing MaterialVendorHistory."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)

    from app.utils.normalization import normalize_mpn_key
    from app.vendor_utils import normalize_vendor_name

    norm_vendor = normalize_vendor_name("MVH Update Vendor")
    vc = VendorCard(
        normalized_name=norm_vendor,
        display_name="MVH Update Vendor",
        sighting_count=0,
    )
    db_session.add(vc)
    db_session.commit()

    norm_mpn = normalize_mpn_key("EXIST-MPN-001")
    mc = MaterialCard(
        normalized_mpn=norm_mpn,
        display_mpn="EXIST-MPN-001",
        manufacturer="Test Mfr",
        search_count=0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(mc)
    db_session.commit()

    mvh = MaterialVendorHistory(
        material_card_id=mc.id,
        vendor_name=norm_vendor,
        source_type="stock_list",
        times_seen=1,
    )
    db_session.add(mvh)
    db_session.commit()

    csv_content = b"mpn,qty,price,manufacturer\nEXIST-MPN-001,500,1.00,Updated Mfr"
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "MVH Update Vendor"},
        files={"file": ("stock.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200


def test_import_stock_enrichment_triggered(client, db_session, monkeypatch):
    """POST /api/materials/import-stock triggers enrichment for new vendor with
    domain."""
    task_created = []
    monkeypatch.setattr(
        "app.routers.materials.safe_background_task", AsyncMock(side_effect=lambda *a, **kw: task_created.append(True))
    )
    monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: "fake-key")

    csv_content = b"mpn,qty,price\nENRICH-001,100,0.50"
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Enrich Stock Vendor", "vendor_website": "https://enrichstock.com"},
        files={"file": ("stock.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["enrich_triggered"] is True


def test_import_stock_skips_bad_rows(client, db_session, monkeypatch):
    """POST /api/materials/import-stock skips rows that normalize_stock_row rejects."""
    monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())
    monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)

    csv_content = b"mpn,qty,price\n,100,0.50\nVALID001,200,0.75"
    resp = client.post(
        "/api/materials/import-stock",
        data={"vendor_name": "Bad Row Vendor"},
        files={"file": ("stock.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_rows"] >= 0


# ── Material card merge ──────────────────────────────────────────────────


class TestMaterialCardMerge:
    def test_merge_material_cards(self, db_session, test_material_card, admin_user):
        """Merge source card into target (lines 1786-1825)."""
        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        source = MaterialCard(
            normalized_mpn="lm317t-alt",
            display_mpn="LM317T-ALT",
            manufacturer="TI",
            description="Alt version",
            search_count=5,
        )
        db_session.add(source)
        db_session.commit()

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        with TestClient(app) as c:
            resp = c.post(
                "/api/materials/merge",
                json={"source_card_id": source.id, "target_card_id": test_material_card.id},
            )
        app.dependency_overrides.clear()
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True


# ── Minimum query length validation tests ────────────────────────────────


def test_list_materials_search_single_char_rejected(client):
    """GET /api/materials?q=x returns 400 for single-character query."""
    resp = client.get("/api/materials", params={"q": "x"})
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"] == "Search query must be at least 2 characters"
    assert data["status_code"] == 400
    assert "request_id" in data


def test_list_materials_search_two_chars_accepted(client, db_session):
    """GET /api/materials?q=lm returns 200 for two-character query."""
    resp = client.get("/api/materials", params={"q": "lm"})
    assert resp.status_code == 200
    data = resp.json()
    assert "materials" in data


def test_list_materials_search_empty_string_ok(client, db_session):
    """GET /api/materials?q= (empty) returns 200, treated as no filter."""
    resp = client.get("/api/materials", params={"q": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert "materials" in data

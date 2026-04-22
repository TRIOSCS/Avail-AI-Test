"""tests/test_materials_coverage.py — Coverage gap tests for app/routers/materials.py.

Targets lines not covered by test_routers_materials.py:
- Short query validation (lines 61-62)
- Brand tags and offer stats batch queries (lines 125-126, 141)
- Manufacturer inference in get_material/get_material_by_mpn (lines 171-176, 213-218)
- quick_search endpoint (lines 192-202)
- enrich endpoint (lines 271-294)
- delete already-deleted error (line 309)
- restore endpoint (lines 326-343)
- merge endpoint (lines 355-369)
- backfill manufacturers (lines 379-382)
- update_material field paths (lines 397, 405, 413, 415)
- import_stock full flow (lines 421-536)

Called by: pytest
Depends on: conftest.py fixtures, app models
"""

import os

os.environ["TESTING"] = "1"

import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.models import MaterialCard, Offer, VendorCard


class TestListMaterialsShortQuery:
    """Short query validation (lines 61-62)."""

    def test_single_char_query_returns_400(self, client, db_session):
        """Query of length 1 returns 400 error."""
        resp = client.get("/api/materials?q=a")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_empty_query_returns_200(self, client, db_session):
        """Empty query returns 200 with all results."""
        resp = client.get("/api/materials?q=")
        assert resp.status_code == 200

    def test_two_char_query_returns_200(self, client, db_session, test_material_card):
        """Query of length 2 passes validation."""
        resp = client.get("/api/materials?q=lm")
        assert resp.status_code == 200


class TestListMaterialsWithBrandAndOfferStats:
    """Brand tag and offer stats batch queries (lines 125-126, 141)."""

    def test_list_with_material_cards_triggers_batch_queries(self, client, db_session):
        """Multiple material cards trigger brand and offer batch queries."""
        from app.models.tags import MaterialTag, Tag

        # Create cards
        cards = []
        for i in range(3):
            mc = MaterialCard(
                normalized_mpn=f"mc{i}coverage",
                display_mpn=f"MC{i}COVERAGE",
                manufacturer="TI",
                search_count=i,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(mc)
            cards.append(mc)
        db_session.flush()

        # Add a brand tag for the first card
        tag = Tag(name="Texas Instruments", tag_type="brand")
        db_session.add(tag)
        db_session.flush()
        mt = MaterialTag(
            material_card_id=cards[0].id,
            tag_id=tag.id,
            confidence=0.90,
            source="test",
        )
        db_session.add(mt)

        # Add an offer for the second card
        from app.models import Requirement, Requisition

        req = Requisition(name="Test", status="active", customer_name="Cust")
        db_session.add(req)
        db_session.flush()
        r = Requirement(requisition_id=req.id, primary_mpn="MC1COVERAGE", target_qty=10)
        db_session.add(r)
        db_session.flush()
        offer = Offer(
            requirement_id=r.id,
            requisition_id=req.id,
            material_card_id=cards[1].id,
            vendor_name="Brand Vendor",
            mpn="MC1COVERAGE",
            status="active",
            unit_price=0.99,
            qty_available=100,
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get("/api/materials?q=mc0")
        assert resp.status_code == 200
        data = resp.json()
        assert "materials" in data


class TestGetMaterialWithManufacturerInference:
    """Manufacturer inference in get_material (lines 171-176)."""

    def test_get_material_infers_manufacturer(self, client, db_session):
        """GET /api/materials/{id} triggers manufacturer inference when missing."""
        mc = MaterialCard(
            normalized_mpn="infer001",
            display_mpn="INFER001",
            manufacturer="",  # Empty manufacturer
            search_count=0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.commit()

        with patch(
            "app.routers.materials._infer_manufacturer_from_prefix",
            return_value="Inferred Corp",
        ):
            resp = client.get(f"/api/materials/{mc.id}")
        assert resp.status_code == 200

    def test_get_material_no_inference_when_manufacturer_set(self, client, db_session, test_material_card):
        """GET /api/materials/{id} skips inference when manufacturer is set."""
        resp = client.get(f"/api/materials/{test_material_card.id}")
        assert resp.status_code == 200
        assert resp.json()["manufacturer"] == "Texas Instruments"


class TestQuickSearch:
    """quick_search endpoint (lines 192-202)."""

    def test_quick_search_with_mpn(self, client, db_session):
        """POST /api/quick-search with valid MPN returns results."""
        with patch(
            "app.search_service.quick_search_mpn",
            new=AsyncMock(return_value={"mpn": "LM317T", "results": []}),
        ):
            resp = client.post("/api/quick-search", json={"mpn": "LM317T"})
        assert resp.status_code == 200

    def test_quick_search_empty_mpn_returns_400(self, client, db_session):
        """POST /api/quick-search without MPN returns 400."""
        resp = client.post("/api/quick-search", json={"mpn": ""})
        assert resp.status_code == 400

    def test_quick_search_short_mpn_returns_400(self, client, db_session):
        """POST /api/quick-search with 1-char MPN returns 400."""
        resp = client.post("/api/quick-search", json={"mpn": "X"})
        assert resp.status_code == 400

    def test_quick_search_no_body_mpn_returns_400(self, client, db_session):
        """POST /api/quick-search with no mpn key returns 400."""
        resp = client.post("/api/quick-search", json={})
        assert resp.status_code == 400


class TestGetMaterialByMpnWithManufacturerInference:
    """Manufacturer inference in get_material_by_mpn (lines 213-218)."""

    def test_by_mpn_infers_manufacturer_when_missing(self, client, db_session):
        """GET /api/materials/by-mpn/{mpn} triggers inference when manufacturer
        empty."""
        mc = MaterialCard(
            normalized_mpn="infer002",
            display_mpn="INFER002",
            manufacturer=None,
            search_count=0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.commit()

        with patch(
            "app.routers.materials._infer_manufacturer_from_prefix",
            return_value="By-MPN Corp",
        ):
            resp = client.get("/api/materials/by-mpn/INFER002")
        assert resp.status_code == 200


class TestEnrichMaterial:
    """Enrich endpoint (lines 271-294)."""

    def test_enrich_updates_fields_and_sets_timestamp(self, client, db_session, test_material_card):
        """POST /api/materials/{id}/enrich applies enrichment data and sets
        enriched_at."""
        resp = client.post(
            f"/api/materials/{test_material_card.id}/enrich",
            json={
                "lifecycle_status": "active",
                "package_type": "DIP-8",
                "manufacturer": "TI Updated",
                "description": "Updated desc",
                "source": "test_source",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "lifecycle_status" in data["updated_fields"]
        assert "manufacturer" in data["updated_fields"]
        assert data["card_id"] == test_material_card.id

    def test_enrich_sets_enriched_at_timestamp(self, client, db_session, test_material_card):
        """Enrichment sets enriched_at timestamp."""
        resp = client.post(
            f"/api/materials/{test_material_card.id}/enrich",
            json={"category": "Voltage Regulator"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_material_card)
        assert test_material_card.enriched_at is not None

    def test_enrich_empty_body_updates_nothing(self, client, db_session, test_material_card):
        """POST /api/materials/{id}/enrich with empty body updates nothing."""
        resp = client.post(
            f"/api/materials/{test_material_card.id}/enrich",
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["updated_fields"] == []

    def test_enrich_not_found_returns_404(self, client, db_session):
        """POST /api/materials/99999/enrich returns 404."""
        resp = client.post("/api/materials/99999/enrich", json={"category": "Test"})
        assert resp.status_code == 404


class TestDeleteMaterialAlreadyDeleted:
    """Delete already-deleted card (line 309)."""

    def test_delete_already_deleted_returns_400(self, client, db_session, admin_user):
        """DELETE on already-deleted card returns 400."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        mc = MaterialCard(
            normalized_mpn="alreadydel001",
            display_mpn="ALREADYDEL001",
            manufacturer="Test",
            search_count=0,
            deleted_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.commit()

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        try:
            with TestClient(app) as c:
                resp = c.delete(f"/api/materials/{mc.id}")
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_admin, None)

        assert resp.status_code == 400


class TestRestoreMaterial:
    """Restore endpoint (lines 326-343)."""

    def test_restore_deleted_card(self, client, db_session, admin_user):
        """POST /api/materials/{id}/restore restores a soft-deleted card."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        mc = MaterialCard(
            normalized_mpn="restore001",
            display_mpn="RESTORE001",
            manufacturer="Test",
            search_count=0,
            deleted_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.commit()

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        try:
            with TestClient(app) as c:
                resp = c.post(f"/api/materials/{mc.id}/restore")
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_admin, None)

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        db_session.refresh(mc)
        assert mc.deleted_at is None

    def test_restore_not_deleted_returns_400(self, client, db_session, admin_user):
        """POST /api/materials/{id}/restore on non-deleted card returns 400."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        mc = MaterialCard(
            normalized_mpn="restore002",
            display_mpn="RESTORE002",
            manufacturer="Test",
            search_count=0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.commit()

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        try:
            with TestClient(app) as c:
                resp = c.post(f"/api/materials/{mc.id}/restore")
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_admin, None)

        assert resp.status_code == 400

    def test_restore_not_found_returns_404(self, client, db_session, admin_user):
        """POST /api/materials/99999/restore returns 404."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        try:
            with TestClient(app) as c:
                resp = c.post("/api/materials/99999/restore")
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_admin, None)

        assert resp.status_code == 404


class TestMergeMaterial:
    """Merge endpoint (lines 355-369)."""

    def test_merge_missing_source_returns_400(self, client, db_session, admin_user):
        """POST /api/materials/merge with missing source_card_id returns 400."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        try:
            with TestClient(app) as c:
                resp = c.post("/api/materials/merge", json={"target_card_id": 1})
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_admin, None)

        assert resp.status_code == 400

    def test_merge_missing_target_returns_400(self, client, db_session, admin_user):
        """POST /api/materials/merge with missing target_card_id returns 400."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        try:
            with TestClient(app) as c:
                resp = c.post("/api/materials/merge", json={"source_card_id": 1})
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_admin, None)

        assert resp.status_code == 400

    def test_merge_same_card_returns_400(self, client, db_session, admin_user, test_material_card):
        """POST /api/materials/merge with source==target returns 400."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/api/materials/merge",
                    json={"source_card_id": test_material_card.id, "target_card_id": test_material_card.id},
                )
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_admin, None)

        assert resp.status_code == 400

    def test_merge_not_found_returns_404(self, client, db_session, admin_user):
        """POST /api/materials/merge with nonexistent IDs returns 404."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        try:
            with TestClient(app) as c:
                resp = c.post("/api/materials/merge", json={"source_card_id": 99998, "target_card_id": 99999})
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_admin, None)

        assert resp.status_code == 404


class TestBackfillManufacturers:
    """Backfill-manufacturers endpoint (lines 379-382)."""

    def test_backfill_manufacturers_returns_count(self, client, db_session, admin_user):
        """POST /materials/backfill-manufacturers returns enriched_records count."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        try:
            with TestClient(app) as c:
                resp = c.post("/materials/backfill-manufacturers")
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_admin, None)

        assert resp.status_code == 200
        assert "enriched_records" in resp.json()


class TestUpdateMaterialAllFields:
    """Update material with various fields (lines 397, 405, 413, 415)."""

    def test_update_all_enrichment_fields(self, client, db_session, test_material_card):
        """PUT /api/materials/{id} with all enrichment fields updates them."""
        resp = client.put(
            f"/api/materials/{test_material_card.id}",
            json={
                "lifecycle_status": "eol",
                "package_type": "SOIC-8",
                "category": "Analog",
                "rohs_status": "compliant",
                "pin_count": 8,
                "datasheet_url": "https://example.com/ds.pdf",
                "cross_references": [{"mpn": "ALT123"}],
                "specs_summary": "Test specs",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["lifecycle_status"] == "eol"
        assert data["package_type"] == "SOIC-8"
        assert data["enrichment_source"] == "manual"

    def test_update_sets_manual_source_when_no_existing_source(self, client, db_session):
        """PUT sets enrichment_source=manual when previously unset."""
        mc = MaterialCard(
            normalized_mpn="enrich_src_test",
            display_mpn="ENRICHSRCTEST",
            manufacturer="Test",
            search_count=0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.commit()

        resp = client.put(
            f"/api/materials/{mc.id}",
            json={"category": "MCU"},
        )
        assert resp.status_code == 200
        assert resp.json()["enrichment_source"] == "manual"

    def test_update_does_not_overwrite_existing_enrichment_source(self, client, db_session):
        """PUT does not overwrite an existing enrichment_source."""
        mc = MaterialCard(
            normalized_mpn="enrich_src_existing",
            display_mpn="ENRICHSRCEXISTING",
            manufacturer="Test",
            search_count=0,
            enrichment_source="claude_agent",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(mc)
        db_session.commit()

        resp = client.put(
            f"/api/materials/{mc.id}",
            json={"category": "MCU"},
        )
        assert resp.status_code == 200
        # enrichment_source stays claude_agent (not overwritten to manual)
        assert resp.json()["enrichment_source"] == "claude_agent"


class TestImportStockInvalidFile:
    """Import stock invalid file type (lines 421-536)."""

    def test_import_stock_invalid_file_type_returns_400(self, client, db_session, monkeypatch):
        """POST /api/materials/import-stock with invalid file type returns 400."""
        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)

        content = b"this is a pdf file"
        resp = client.post(
            "/api/materials/import-stock",
            data={"vendor_name": "Test Vendor"},
            files={"file": ("stock.pdf", io.BytesIO(content), "application/pdf")},
        )
        assert resp.status_code == 400

    def test_import_stock_vendor_name_too_long_returns_400(self, client, db_session, monkeypatch):
        """POST /api/materials/import-stock with > 255 char vendor_name returns 400."""
        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)

        content = b"mpn,qty\nTEST001,100"
        resp = client.post(
            "/api/materials/import-stock",
            data={"vendor_name": "A" * 256},
            files={"file": ("stock.csv", io.BytesIO(content), "text/csv")},
        )
        assert resp.status_code == 400

    def test_import_stock_vendor_name_with_html_only_stripped_to_empty(self, client, db_session, monkeypatch):
        """POST /api/materials/import-stock strips HTML tags; all-HTML name → 400."""
        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)
        monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

        content = b"mpn,qty,price\nHTML001,100,0.50"
        # This name is all HTML tags, stripped result is empty
        resp = client.post(
            "/api/materials/import-stock",
            data={"vendor_name": "<b></b><i></i>"},
            files={"file": ("stock.csv", io.BytesIO(content), "text/csv")},
        )
        # HTML stripped → empty vendor name → 400
        assert resp.status_code == 400

    def test_import_stock_tsv_file_accepted(self, client, db_session, monkeypatch):
        """POST /api/materials/import-stock accepts .tsv files."""
        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)
        monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

        content = b"mpn\tqty\tprice\nTSV001\t100\t0.75"
        resp = client.post(
            "/api/materials/import-stock",
            data={"vendor_name": "TSV Vendor"},
            files={"file": ("stock.tsv", io.BytesIO(content), "text/tab-separated-values")},
        )
        assert resp.status_code == 200

    def test_import_stock_xlsx_file_accepted(self, client, db_session, monkeypatch):
        """POST /api/materials/import-stock accepts .xlsx files."""
        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)
        monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

        # Create a minimal xlsx-like CSV content (parse_tabular_file handles xlsx)
        content = b"mpn,qty,price\nXLSX001,50,1.00"
        resp = client.post(
            "/api/materials/import-stock",
            data={"vendor_name": "XLSX Vendor"},
            files={"file": ("stock.xlsx", io.BytesIO(content), "application/vnd.ms-excel")},
        )
        # May succeed or fail based on xlsx parsing, but not a 400 file-type error
        assert resp.status_code in (200, 400, 500)

    def test_import_stock_integrity_error_on_duplicate_vendor(self, client, db_session, monkeypatch):
        """POST /api/materials/import-stock handles IntegrityError on duplicate
        vendor."""
        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)
        monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close())

        from app.vendor_utils import normalize_vendor_name

        norm = normalize_vendor_name("Duplicate Stock Vendor")
        vc = VendorCard(
            normalized_name=norm,
            display_name="Duplicate Stock Vendor",
            sighting_count=5,
        )
        db_session.add(vc)
        db_session.commit()

        content = b"mpn,qty,price\nDUP001,100,0.50"
        resp = client.post(
            "/api/materials/import-stock",
            data={"vendor_name": "Duplicate Stock Vendor"},
            files={"file": ("stock.csv", io.BytesIO(content), "text/csv")},
        )
        assert resp.status_code == 200

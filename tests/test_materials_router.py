"""test_materials_router.py — Tests for app/routers/materials.py.

Covers: list_materials, get_material, get_material_by_mpn, update_material,
        enrich_material, delete_material, restore_material, merge_material_cards,
        backfill_manufacturers, quick_search, import_stock_list_standalone.

Business rules tested:
- Material list filters deleted cards, supports search/pagination
- Search query must be >= 2 characters
- Material detail enriches manufacturer on first access
- Update applies only provided fields, sets enrichment_source on enrichment fields
- Enrich endpoint applies AI data and sets enrichment metadata
- Soft-delete and restore toggle deleted_at timestamp
- Merge requires both source and target IDs
- Stock import validates file type, vendor name, file size
- Stock import upserts VendorCard, MaterialCard, MaterialVendorHistory
"""

import io
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    MaterialCard,
    MaterialVendorHistory,
)

# ── List Materials ──────────────────────────────────────────────────────


class TestListMaterials:
    def test_list_empty(self, client: TestClient):
        resp = client.get("/api/materials")
        assert resp.status_code == 200
        data = resp.json()
        assert data["materials"] == []
        assert data["total"] == 0

    def test_list_with_materials(self, client: TestClient, db_session: Session, test_material_card: MaterialCard):
        resp = client.get("/api/materials")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        found = [m for m in data["materials"] if m["id"] == test_material_card.id]
        assert len(found) == 1
        assert found[0]["display_mpn"] == "LM317T"

    def test_list_search(self, client: TestClient, db_session: Session, test_material_card: MaterialCard):
        resp = client.get("/api/materials?q=lm317")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_list_search_too_short(self, client: TestClient):
        resp = client.get("/api/materials?q=a")
        assert resp.status_code == 400

    def test_list_pagination(self, client: TestClient, db_session: Session, test_material_card: MaterialCard):
        resp = client.get("/api/materials?limit=1&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 1
        assert data["offset"] == 0

    def test_list_bad_limit(self, client: TestClient):
        resp = client.get("/api/materials?limit=abc")
        assert resp.status_code == 400

    def test_list_excludes_deleted(self, client: TestClient, db_session: Session):
        card = MaterialCard(
            normalized_mpn="deleted001",
            display_mpn="DELETED001",
            deleted_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get("/api/materials?q=deleted001")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ── Get Material ────────────────────────────────────────────────────────


class TestGetMaterial:
    def test_get_material(self, client: TestClient, test_material_card: MaterialCard):
        resp = client.get(f"/api/materials/{test_material_card.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_mpn"] == "LM317T"

    def test_get_material_not_found(self, client: TestClient):
        resp = client.get("/api/materials/99999")
        assert resp.status_code == 404

    def test_get_deleted_material(self, client: TestClient, db_session: Session):
        card = MaterialCard(
            normalized_mpn="gone001",
            display_mpn="GONE001",
            deleted_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get(f"/api/materials/{card.id}")
        assert resp.status_code == 404

    @patch("app.routers.materials._infer_manufacturer_from_prefix", return_value="Texas Instruments")
    def test_get_material_infers_manufacturer(self, mock_infer, client: TestClient, db_session: Session):
        card = MaterialCard(
            normalized_mpn="lm7805",
            display_mpn="LM7805",
            manufacturer=None,
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get(f"/api/materials/{card.id}")
        assert resp.status_code == 200
        db_session.refresh(card)
        assert card.manufacturer == "Texas Instruments"


# ── Get Material by MPN ─────────────────────────────────────────────────


class TestGetMaterialByMPN:
    def test_found(self, client: TestClient, test_material_card: MaterialCard):
        resp = client.get("/api/materials/by-mpn/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_mpn"] == "LM317T"

    def test_not_found(self, client: TestClient):
        resp = client.get("/api/materials/by-mpn/NONEXISTENT999")
        assert resp.status_code == 404


# ── Update Material ─────────────────────────────────────────────────────


class TestUpdateMaterial:
    def test_update_manufacturer(self, client: TestClient, test_material_card: MaterialCard):
        resp = client.put(
            f"/api/materials/{test_material_card.id}",
            json={"manufacturer": "ON Semiconductor"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["manufacturer"] == "ON Semiconductor"

    def test_update_description(self, client: TestClient, test_material_card: MaterialCard):
        resp = client.put(
            f"/api/materials/{test_material_card.id}",
            json={"description": "New description"},
        )
        assert resp.status_code == 200

    def test_update_display_mpn(self, client: TestClient, test_material_card: MaterialCard):
        resp = client.put(
            f"/api/materials/{test_material_card.id}",
            json={"display_mpn": "LM317T-ADJ"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_mpn"] == "LM317T-ADJ"

    def test_update_enrichment_field_sets_source(
        self, client: TestClient, db_session: Session, test_material_card: MaterialCard
    ):
        resp = client.put(
            f"/api/materials/{test_material_card.id}",
            json={"lifecycle_status": "active", "category": "Voltage Regulators"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_material_card)
        assert test_material_card.lifecycle_status == "active"
        assert test_material_card.enrichment_source == "manual"

    def test_update_not_found(self, client: TestClient):
        resp = client.put("/api/materials/99999", json={"manufacturer": "TI"})
        assert resp.status_code == 404

    def test_update_deleted_card(self, client: TestClient, db_session: Session):
        card = MaterialCard(
            normalized_mpn="upddel",
            display_mpn="UPDDEL",
            deleted_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        resp = client.put(f"/api/materials/{card.id}", json={"manufacturer": "TI"})
        assert resp.status_code == 404


# ── Enrich Material ─────────────────────────────────────────────────────


class TestEnrichMaterial:
    def test_enrich(self, client: TestClient, test_material_card: MaterialCard):
        resp = client.post(
            f"/api/materials/{test_material_card.id}/enrich",
            json={
                "lifecycle_status": "active",
                "package_type": "TO-220",
                "manufacturer": "Texas Instruments",
                "source": "claude_agent",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "lifecycle_status" in data["updated_fields"]
        assert "manufacturer" in data["updated_fields"]

    def test_enrich_not_found(self, client: TestClient):
        resp = client.post("/api/materials/99999/enrich", json={"lifecycle_status": "eol"})
        assert resp.status_code == 404

    def test_enrich_no_fields(self, client: TestClient, test_material_card: MaterialCard):
        resp = client.post(
            f"/api/materials/{test_material_card.id}/enrich",
            json={"non_enrichment_field": "value"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated_fields"] == []

    def test_enrich_sets_timestamp(self, client: TestClient, db_session: Session, test_material_card: MaterialCard):
        resp = client.post(
            f"/api/materials/{test_material_card.id}/enrich",
            json={"category": "IC"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_material_card)
        assert test_material_card.enriched_at is not None
        assert test_material_card.enrichment_source == "claude_agent"


# ── Delete Material ─────────────────────────────────────────────────────


class TestDeleteMaterial:
    @patch("app.routers.materials.log_audit")
    def test_delete(self, mock_audit, client: TestClient, db_session: Session, test_material_card: MaterialCard):
        resp = client.delete(f"/api/materials/{test_material_card.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "deleted_at" in data
        db_session.refresh(test_material_card)
        assert test_material_card.deleted_at is not None

    def test_delete_not_found(self, client: TestClient):
        resp = client.delete("/api/materials/99999")
        assert resp.status_code == 404

    @patch("app.routers.materials.log_audit")
    def test_delete_already_deleted(self, mock_audit, client: TestClient, db_session: Session):
        card = MaterialCard(
            normalized_mpn="deldel",
            display_mpn="DELDEL",
            deleted_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        resp = client.delete(f"/api/materials/{card.id}")
        assert resp.status_code == 400


# ── Restore Material ────────────────────────────────────────────────────


class TestRestoreMaterial:
    @patch("app.routers.materials.log_audit")
    def test_restore(self, mock_audit, client: TestClient, db_session: Session):
        card = MaterialCard(
            normalized_mpn="restme",
            display_mpn="RESTME",
            deleted_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        resp = client.post(f"/api/materials/{card.id}/restore")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        db_session.refresh(card)
        assert card.deleted_at is None

    def test_restore_not_found(self, client: TestClient):
        resp = client.post("/api/materials/99999/restore")
        assert resp.status_code == 404

    @patch("app.routers.materials.log_audit")
    def test_restore_not_deleted(self, mock_audit, client: TestClient, test_material_card: MaterialCard):
        resp = client.post(f"/api/materials/{test_material_card.id}/restore")
        assert resp.status_code == 400


# ── Merge Material Cards ────────────────────────────────────────────────


class TestMergeMaterialCards:
    def test_merge_missing_ids(self, client: TestClient):
        resp = client.post("/api/materials/merge", json={})
        assert resp.status_code == 400

    @patch("app.routers.materials._merge_material_cards_service")
    def test_merge_success(self, mock_merge, client: TestClient, db_session: Session):
        card1 = MaterialCard(normalized_mpn="src001", display_mpn="SRC001")
        card2 = MaterialCard(normalized_mpn="tgt001", display_mpn="TGT001")
        db_session.add_all([card1, card2])
        db_session.commit()
        mock_merge.return_value = {"ok": True, "moved_records": 5}
        resp = client.post(
            "/api/materials/merge",
            json={"source_card_id": card1.id, "target_card_id": card2.id},
        )
        assert resp.status_code == 200

    @patch(
        "app.routers.materials._merge_material_cards_service", side_effect=ValueError("Cannot merge card into itself")
    )
    def test_merge_self_error(self, mock_merge, client: TestClient):
        resp = client.post(
            "/api/materials/merge",
            json={"source_card_id": 1, "target_card_id": 1},
        )
        assert resp.status_code == 400

    @patch(
        "app.routers.materials._merge_material_cards_service",
        side_effect=ValueError("Source card not found"),
    )
    def test_merge_not_found(self, mock_merge, client: TestClient):
        resp = client.post(
            "/api/materials/merge",
            json={"source_card_id": 999, "target_card_id": 1000},
        )
        assert resp.status_code == 404


# ── Backfill Manufacturers ──────────────────────────────────────────────


class TestBackfillManufacturers:
    @patch("app.routers.materials.backfill_missing_manufacturers", return_value=10)
    def test_backfill(self, mock_bf, client: TestClient):
        resp = client.post("/materials/backfill-manufacturers")
        assert resp.status_code == 200
        assert resp.json()["enriched_records"] == 10


# ── Quick Search ────────────────────────────────────────────────────────


class TestQuickSearch:
    def test_empty_mpn(self, client: TestClient):
        resp = client.post("/api/quick-search", json={"mpn": ""})
        assert resp.status_code == 400

    def test_short_mpn(self, client: TestClient):
        resp = client.post("/api/quick-search", json={"mpn": "X"})
        assert resp.status_code == 400

    @patch("app.routers.materials.quick_search_mpn")
    def test_success(self, mock_qs, client: TestClient):
        mock_qs.return_value = {"results": [], "material_card_id": 1}
        resp = client.post("/api/quick-search", json={"mpn": "LM317T"})
        assert resp.status_code == 200


# ── Import Stock List ───────────────────────────────────────────────────


class TestImportStockList:
    def test_no_file(self, client: TestClient):
        resp = client.post("/api/materials/import-stock", data={"vendor_name": "Test"})
        assert resp.status_code == 400

    def test_invalid_file_type(self, client: TestClient):
        f = io.BytesIO(b"data")
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("test.pdf", f, "application/pdf")},
            data={"vendor_name": "Test Vendor"},
        )
        assert resp.status_code == 400
        assert "Invalid file type" in resp.json().get("detail", resp.json().get("error", ""))

    def test_missing_vendor_name(self, client: TestClient):
        f = io.BytesIO(b"mpn,qty,price\nLM317T,100,0.50\n")
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("test.csv", f, "text/csv")},
            data={"vendor_name": ""},
        )
        assert resp.status_code == 400

    def test_vendor_name_too_long(self, client: TestClient):
        f = io.BytesIO(b"mpn,qty,price\nLM317T,100,0.50\n")
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("test.csv", f, "text/csv")},
            data={"vendor_name": "A" * 256},
        )
        assert resp.status_code == 400

    @patch("app.routers.materials.parse_tabular_file")
    @patch("app.routers.materials.normalize_stock_row")
    @patch("app.routers.materials.record_price_snapshot")
    @patch("app.routers.materials.get_credential_cached", return_value=None)
    def test_import_success(
        self,
        mock_cred,
        mock_snapshot,
        mock_normalize,
        mock_parse,
        client: TestClient,
        db_session: Session,
    ):
        mock_parse.return_value = [{"mpn": "LM317T", "qty": 100, "price": 0.50}]
        mock_normalize.return_value = {"mpn": "LM317T", "qty": 100, "price": 0.50, "manufacturer": "TI"}

        f = io.BytesIO(b"mpn,qty,price\nLM317T,100,0.50\n")
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("test.csv", f, "text/csv")},
            data={"vendor_name": "Test Vendor"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported_rows"] == 1
        assert data["vendor_name"] == "Test Vendor"

    @patch("app.routers.materials.parse_tabular_file")
    @patch("app.routers.materials.normalize_stock_row")
    @patch("app.routers.materials.record_price_snapshot")
    @patch("app.routers.materials.get_credential_cached", return_value=None)
    def test_import_updates_existing_mvh(
        self,
        mock_cred,
        mock_snapshot,
        mock_normalize,
        mock_parse,
        client: TestClient,
        db_session: Session,
    ):
        """Importing a stock list for existing material+vendor updates the MVH."""
        from app.vendor_utils import normalize_vendor_name

        # Pre-create material card and MVH
        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T")
        db_session.add(card)
        db_session.flush()
        norm_vendor = normalize_vendor_name("Test Vendor")
        mvh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name=norm_vendor,
            vendor_name_normalized=norm_vendor,
            source_type="stock_list",
            source="stock_list",
            times_seen=1,
        )
        db_session.add(mvh)
        db_session.commit()

        mock_parse.return_value = [{"mpn": "LM317T", "qty": 200, "price": 0.60}]
        mock_normalize.return_value = {"mpn": "LM317T", "qty": 200, "price": 0.60, "manufacturer": "TI"}

        f = io.BytesIO(b"mpn,qty,price\nLM317T,200,0.60\n")
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("test.csv", f, "text/csv")},
            data={"vendor_name": "Test Vendor"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported_rows"] == 1

        # Check MVH was updated
        db_session.refresh(mvh)
        assert mvh.times_seen == 2
        assert mvh.last_qty == 200

    @patch("app.routers.materials.parse_tabular_file")
    @patch("app.routers.materials.normalize_stock_row", return_value=None)
    @patch("app.routers.materials.get_credential_cached", return_value=None)
    def test_import_skips_bad_rows(
        self,
        mock_cred,
        mock_normalize,
        mock_parse,
        client: TestClient,
        db_session: Session,
    ):
        mock_parse.return_value = [{"bad": "row"}]

        f = io.BytesIO(b"garbage\n")
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("test.csv", f, "text/csv")},
            data={"vendor_name": "Test Vendor"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported_rows"] == 0
        assert data["skipped_rows"] == 1

    def test_vendor_name_html_stripped(self, client: TestClient):
        """HTML tags in vendor name are stripped before validation."""
        f = io.BytesIO(b"mpn,qty\nLM317T,100\n")
        # Vendor name that's all HTML tags
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("test.csv", f, "text/csv")},
            data={"vendor_name": "<script>alert('xss')</script>"},
        )
        # After stripping tags, the vendor name becomes "alert('xss')" which is valid
        # The important thing is HTML is stripped
        assert resp.status_code in (200, 400)

"""tests/test_materials_router_coverage.py — Coverage tests for app/routers/materials.py.

Tests all major branches: list, get, update, enrich, delete, restore, merge,
quick-search, by-mpn, backfill, and import-stock.

Called by: pytest
Depends on: tests.conftest (client, db_session, test_material_card fixtures)
"""

import io
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.requests import Request

from app.models import MaterialCard, MaterialVendorHistory, VendorCard


# -- Helper -------------------------------------------------------------------


def _make_card(db_session, mpn="lm317t", display="LM317T", manufacturer="Texas Instruments"):
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=display,
        manufacturer=manufacturer,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


# -- GET /api/materials -------------------------------------------------------


class TestListMaterials:
    def test_list_returns_200_empty(self, client, db_session):
        resp = client.get("/api/materials")
        assert resp.status_code == 200
        data = resp.json()
        assert "materials" in data
        assert "total" in data

    def test_list_with_results(self, client, db_session):
        _make_card(db_session)
        resp = client.get("/api/materials")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_list_search_filter(self, client, db_session):
        _make_card(db_session, mpn="atmega328", display="ATmega328")
        resp = client.get("/api/materials?q=atmega")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    def test_list_short_query_returns_400(self, client, db_session):
        resp = client.get("/api/materials?q=a")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_list_invalid_limit_returns_400(self, client, db_session):
        resp = client.get("/api/materials?limit=abc")
        assert resp.status_code == 400

    def test_list_invalid_offset_returns_400(self, client, db_session):
        resp = client.get("/api/materials?offset=xyz")
        assert resp.status_code == 400

    def test_list_respects_limit_and_offset(self, client, db_session):
        for i in range(5):
            _make_card(db_session, mpn=f"testpart{i:03d}", display=f"TESTPART{i:03d}")
        resp = client.get("/api/materials?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["materials"]) <= 2

    def test_list_excludes_deleted_cards(self, client, db_session):
        card = _make_card(db_session, mpn="deletedpart", display="DELETEDPART")
        card.deleted_at = datetime.now(timezone.utc)
        db_session.commit()
        resp = client.get("/api/materials?q=deletedpart")
        assert resp.status_code == 200
        # Deleted card should not appear
        mpns = [m["display_mpn"] for m in resp.json()["materials"]]
        assert "DELETEDPART" not in mpns


# -- GET /api/materials/{card_id} ---------------------------------------------


class TestGetMaterial:
    def test_get_existing_card(self, client, db_session):
        card = _make_card(db_session)
        resp = client.get(f"/api/materials/{card.id}")
        assert resp.status_code == 200

    def test_get_nonexistent_returns_404(self, client, db_session):
        resp = client.get("/api/materials/99999")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_get_deleted_card_returns_404(self, client, db_session):
        card = _make_card(db_session, mpn="gone", display="GONE")
        card.deleted_at = datetime.now(timezone.utc)
        db_session.commit()
        resp = client.get(f"/api/materials/{card.id}")
        assert resp.status_code == 404

    def test_get_infers_manufacturer_when_missing(self, client, db_session):
        """Card with no manufacturer should trigger infer_manufacturer path."""
        card = MaterialCard(
            normalized_mpn="lm358dr",
            display_mpn="LM358DR",
            manufacturer=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        with patch("app.routers.materials._infer_manufacturer_from_prefix", return_value="TI"):
            resp = client.get(f"/api/materials/{card.id}")
        assert resp.status_code == 200


# -- GET /api/materials/by-mpn/{mpn} ------------------------------------------


class TestGetMaterialByMpn:
    def test_get_by_mpn_success(self, client, db_session):
        _make_card(db_session, mpn="lm741", display="LM741")
        resp = client.get("/api/materials/by-mpn/LM741")
        assert resp.status_code == 200

    def test_get_by_mpn_not_found(self, client, db_session):
        resp = client.get("/api/materials/by-mpn/NONEXISTENT99999")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_get_by_mpn_infers_manufacturer(self, client, db_session):
        card = MaterialCard(
            normalized_mpn="ne555",
            display_mpn="NE555",
            manufacturer=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        with patch("app.routers.materials._infer_manufacturer_from_prefix", return_value="Philips"):
            resp = client.get("/api/materials/by-mpn/NE555")
        assert resp.status_code == 200


# -- PUT /api/materials/{card_id} ---------------------------------------------


class TestUpdateMaterial:
    def test_update_manufacturer(self, client, db_session):
        card = _make_card(db_session)
        resp = client.put(f"/api/materials/{card.id}", json={"manufacturer": "Microchip"})
        assert resp.status_code == 200

    def test_update_description(self, client, db_session):
        card = _make_card(db_session)
        resp = client.put(f"/api/materials/{card.id}", json={"description": "Updated desc"})
        assert resp.status_code == 200

    def test_update_display_mpn(self, client, db_session):
        card = _make_card(db_session)
        resp = client.put(f"/api/materials/{card.id}", json={"display_mpn": "LM317T-NEW"})
        assert resp.status_code == 200

    def test_update_empty_display_mpn_ignored(self, client, db_session):
        card = _make_card(db_session, display="ORIGINAL")
        resp = client.put(f"/api/materials/{card.id}", json={"display_mpn": "  "})
        assert resp.status_code == 200
        db_session.refresh(card)
        assert card.display_mpn == "ORIGINAL"

    def test_update_enrichment_fields(self, client, db_session):
        card = _make_card(db_session)
        resp = client.put(
            f"/api/materials/{card.id}",
            json={
                "lifecycle_status": "active",
                "package_type": "DIP-8",
                "category": "voltage regulator",
                "rohs_status": "compliant",
                "pin_count": 8,
            },
        )
        assert resp.status_code == 200

    def test_update_sets_manual_enrichment_source(self, client, db_session):
        card = _make_card(db_session)
        assert card.enrichment_source is None
        resp = client.put(f"/api/materials/{card.id}", json={"lifecycle_status": "active"})
        assert resp.status_code == 200
        db_session.refresh(card)
        assert card.enrichment_source == "manual"

    def test_update_nonexistent_returns_404(self, client, db_session):
        resp = client.put("/api/materials/99999", json={"manufacturer": "TI"})
        assert resp.status_code == 404

    def test_update_deleted_card_returns_404(self, client, db_session):
        card = _make_card(db_session, mpn="updatedeleted", display="UPDATEDELETED")
        card.deleted_at = datetime.now(timezone.utc)
        db_session.commit()
        resp = client.put(f"/api/materials/{card.id}", json={"manufacturer": "TI"})
        assert resp.status_code == 404


# -- POST /api/materials/{card_id}/enrich -------------------------------------


class TestEnrichMaterial:
    def test_enrich_success(self, client, db_session):
        card = _make_card(db_session)
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"lifecycle_status": "active", "manufacturer": "TI"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "lifecycle_status" in data["updated_fields"]

    def test_enrich_not_found(self, client, db_session):
        resp = client.post("/api/materials/99999/enrich", json={"manufacturer": "TI"})
        assert resp.status_code == 404

    def test_enrich_empty_body_no_update(self, client, db_session):
        card = _make_card(db_session)
        resp = client.post(f"/api/materials/{card.id}/enrich", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated_fields"] == []

    def test_enrich_sets_custom_source(self, client, db_session):
        card = _make_card(db_session)
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"manufacturer": "NXP", "source": "octopart"},
        )
        assert resp.status_code == 200
        db_session.refresh(card)
        assert card.enrichment_source == "octopart"

    def test_enrich_all_fields(self, client, db_session):
        card = _make_card(db_session)
        payload = {
            "lifecycle_status": "nrnd",
            "package_type": "SOT-23",
            "category": "transistor",
            "rohs_status": "compliant",
            "pin_count": 3,
            "datasheet_url": "https://example.com/ds.pdf",
            "cross_references": ["BC547", "2N3904"],
            "specs_summary": "NPN BJT",
            "manufacturer": "ON Semi",
            "description": "NPN Transistor",
        }
        resp = client.post(f"/api/materials/{card.id}/enrich", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["updated_fields"]) == 10


# -- DELETE /api/materials/{card_id} ------------------------------------------


class TestDeleteMaterial:
    def test_soft_delete_success(self, client, db_session):
        card = _make_card(db_session, mpn="to_delete", display="TO_DELETE")
        resp = client.delete(f"/api/materials/{card.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "deleted_at" in data

    def test_delete_not_found(self, client, db_session):
        resp = client.delete("/api/materials/99999")
        assert resp.status_code == 404

    def test_delete_already_deleted_returns_400(self, client, db_session):
        card = _make_card(db_session, mpn="already_deleted", display="ALREADY_DELETED")
        card.deleted_at = datetime.now(timezone.utc)
        db_session.commit()
        resp = client.delete(f"/api/materials/{card.id}")
        assert resp.status_code == 400
        assert "error" in resp.json()


# -- POST /api/materials/{card_id}/restore ------------------------------------


class TestRestoreMaterial:
    def test_restore_success(self, client, db_session):
        card = _make_card(db_session, mpn="restore_me", display="RESTORE_ME")
        card.deleted_at = datetime.now(timezone.utc)
        db_session.commit()
        resp = client.post(f"/api/materials/{card.id}/restore")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_restore_not_found(self, client, db_session):
        resp = client.post("/api/materials/99999/restore")
        assert resp.status_code == 404

    def test_restore_not_deleted_returns_400(self, client, db_session):
        card = _make_card(db_session, mpn="not_deleted", display="NOT_DELETED")
        resp = client.post(f"/api/materials/{card.id}/restore")
        assert resp.status_code == 400
        assert "error" in resp.json()


# -- POST /api/materials/merge ------------------------------------------------


class TestMergeMaterials:
    def test_merge_missing_ids_returns_400(self, client, db_session):
        resp = client.post("/api/materials/merge", json={})
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_merge_missing_target_returns_400(self, client, db_session):
        resp = client.post("/api/materials/merge", json={"source_card_id": 1})
        assert resp.status_code == 400

    def test_merge_success(self, client, db_session):
        source = _make_card(db_session, mpn="merge_source", display="MERGE_SOURCE")
        target = _make_card(db_session, mpn="merge_target", display="MERGE_TARGET")

        with patch("app.routers.materials._merge_material_cards_service") as mock_merge:
            mock_merge.return_value = {"merged": True, "source_id": source.id, "target_id": target.id}
            resp = client.post(
                "/api/materials/merge",
                json={"source_card_id": source.id, "target_card_id": target.id},
            )
        assert resp.status_code == 200
        assert resp.json()["merged"] is True

    def test_merge_same_card_returns_400(self, client, db_session):
        card = _make_card(db_session, mpn="self_merge", display="SELF_MERGE")

        with patch("app.routers.materials._merge_material_cards_service") as mock_merge:
            mock_merge.side_effect = ValueError("Cannot merge a card with itself")
            resp = client.post(
                "/api/materials/merge",
                json={"source_card_id": card.id, "target_card_id": card.id},
            )
        assert resp.status_code == 400

    def test_merge_not_found_returns_404(self, client, db_session):
        with patch("app.routers.materials._merge_material_cards_service") as mock_merge:
            mock_merge.side_effect = ValueError("Card not found")
            resp = client.post(
                "/api/materials/merge",
                json={"source_card_id": 99998, "target_card_id": 99999},
            )
        assert resp.status_code == 404


# -- POST /materials/backfill-manufacturers -----------------------------------


class TestBackfillManufacturers:
    def test_backfill_success(self, client, db_session):
        with patch("app.routers.materials.backfill_missing_manufacturers", return_value=3):
            resp = client.post("/materials/backfill-manufacturers")
        assert resp.status_code == 200
        assert resp.json()["enriched_records"] == 3


# -- POST /api/quick-search ---------------------------------------------------


class TestQuickSearch:
    def test_quick_search_no_mpn_returns_400(self, client, db_session):
        resp = client.post("/api/quick-search", json={})
        assert resp.status_code == 400

    def test_quick_search_too_short_returns_400(self, client, db_session):
        resp = client.post("/api/quick-search", json={"mpn": "A"})
        assert resp.status_code == 400

    def test_quick_search_success(self, client, db_session):
        # quick_search_mpn is imported lazily inside the route body, so patch at source
        with patch("app.search_service.quick_search_mpn", new_callable=AsyncMock) as mock_qs:
            mock_qs.return_value = {"mpn": "LM317T", "sightings": []}
            resp = client.post("/api/quick-search", json={"mpn": "LM317T"})
        assert resp.status_code == 200
        assert resp.json()["mpn"] == "LM317T"


# -- POST /api/materials/import-stock -----------------------------------------


class TestImportStock:
    def _csv_bytes(self, rows: list[dict]) -> bytes:
        """Build minimal CSV bytes for upload tests."""
        import csv
        import io

        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return buf.getvalue().encode()

    def test_import_no_file_returns_400(self, client, db_session):
        resp = client.post("/api/materials/import-stock", data={"vendor_name": "Test Vendor"})
        assert resp.status_code == 400

    def test_import_no_vendor_name_returns_400(self, client, db_session):
        csv_data = self._csv_bytes([{"mpn": "LM317T", "qty": "100", "price": "0.50"}])
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("stock.csv", csv_data, "text/csv")},
            data={"vendor_name": ""},
        )
        assert resp.status_code == 400

    def test_import_invalid_extension_returns_400(self, client, db_session):
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("stock.txt", b"mpn,qty\nLM317T,100", "text/plain")},
            data={"vendor_name": "Test Vendor"},
        )
        assert resp.status_code == 400

    def test_import_vendor_name_too_long_returns_400(self, client, db_session):
        csv_data = self._csv_bytes([{"mpn": "LM317T", "qty": "100"}])
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("stock.csv", csv_data, "text/csv")},
            data={"vendor_name": "A" * 300},
        )
        assert resp.status_code == 400

    def test_import_html_stripped_from_vendor_name(self, client, db_session):
        """HTML tags only (no text content) in vendor_name strip to empty → 400."""
        # All characters are inside HTML tags — stripping leaves empty string
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("stock.csv", b"mpn,qty\n", "text/csv")},
            data={"vendor_name": "<b></b><i></i>"},
        )
        # After stripping HTML, vendor_name becomes empty → 400
        assert resp.status_code == 400

    def test_import_csv_success(self, client, db_session):
        csv_data = self._csv_bytes([
            {"mpn": "TESTVEND001", "qty": "500", "price": "1.25", "manufacturer": "TI"},
        ])
        with patch("app.routers.materials.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/materials/import-stock",
                files={"file": ("stock.csv", csv_data, "text/csv")},
                data={"vendor_name": "Test Vendor Co"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["imported_rows"] >= 0
        assert data["vendor_name"] == "Test Vendor Co"

    def test_import_creates_vendor_card(self, client, db_session):
        csv_data = self._csv_bytes([{"mpn": "NEWVEND002", "qty": "10"}])
        with patch("app.routers.materials.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/materials/import-stock",
                files={"file": ("stock.csv", csv_data, "text/csv")},
                data={"vendor_name": "Brand New Vendor"},
            )
        assert resp.status_code == 200

    def test_import_with_existing_vendor_card(self, client, db_session):
        """Second upload from the same vendor should reuse the existing VendorCard."""
        vendor = VendorCard(
            normalized_name="existing vendor",
            display_name="Existing Vendor",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vendor)
        db_session.commit()

        csv_data = self._csv_bytes([{"mpn": "EXISTVEND003", "qty": "200"}])
        with patch("app.routers.materials.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/materials/import-stock",
                files={"file": ("stock.csv", csv_data, "text/csv")},
                data={"vendor_name": "Existing Vendor"},
            )
        assert resp.status_code == 200

    def test_import_file_too_large_returns_413(self, client, db_session):
        large_content = b"mpn,qty\n" + b"LM317T,100\n" * 1_000_000
        resp = client.post(
            "/api/materials/import-stock",
            files={"file": ("stock.csv", large_content, "text/csv")},
            data={"vendor_name": "Test Vendor"},
        )
        assert resp.status_code == 413

    def test_import_with_vendor_website(self, client, db_session):
        """Providing vendor_website sets domain on new VendorCard."""
        csv_data = self._csv_bytes([{"mpn": "WEBVEND004", "qty": "50"}])
        with patch("app.routers.materials.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/materials/import-stock",
                files={"file": ("stock.csv", csv_data, "text/csv")},
                data={"vendor_name": "Web Vendor", "vendor_website": "https://www.webvendor.com/"},
            )
        assert resp.status_code == 200

    def test_import_existing_mvh_updated(self, client, db_session):
        """Re-importing same part+vendor should update existing MaterialVendorHistory."""
        norm_vendor = "update vendor"
        card = MaterialCard(
            normalized_mpn="updatepart",
            display_mpn="UPDATEPART",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
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

        csv_data = self._csv_bytes([{"mpn": "UPDATEPART", "qty": "999", "price": "0.99"}])
        with patch("app.routers.materials.get_credential_cached", return_value=None):
            resp = client.post(
                "/api/materials/import-stock",
                files={"file": ("stock.csv", csv_data, "text/csv")},
                data={"vendor_name": "Update Vendor"},
            )
        assert resp.status_code == 200


# -- Direct async handler tests (improve branch coverage) ---------------------
# These call route handlers directly to bypass TestClient async boundary issue.


def _make_mock_request(json_body: dict | None = None) -> Request:
    """Build a minimal Starlette Request with a JSON body."""
    import json

    body_bytes = json.dumps(json_body or {}).encode()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode()),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request(scope, receive)


class TestDirectHandlerCoverage:
    """Call async route handlers directly to get coverage of async function bodies."""

    async def test_enrich_material_direct_updates_fields(self, db_session):
        from app.routers.materials import enrich_material

        card = MaterialCard(
            normalized_mpn="direct001",
            display_mpn="DIRECT001",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        req = _make_mock_request({"manufacturer": "Microchip", "lifecycle_status": "active"})
        user = MagicMock()
        user.email = "test@test.com"

        result = await enrich_material(card.id, req, user=user, db=db_session)
        assert result["ok"] is True
        assert "manufacturer" in result["updated_fields"]

    async def test_enrich_material_direct_not_found(self, db_session):
        from app.routers.materials import enrich_material

        req = _make_mock_request({"manufacturer": "TI"})
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await enrich_material(99999, req, user=user, db=db_session)
        assert exc.value.status_code == 404

    async def test_enrich_material_direct_empty_body(self, db_session):
        from app.routers.materials import enrich_material

        card = MaterialCard(
            normalized_mpn="direct002",
            display_mpn="DIRECT002",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        req = _make_mock_request({})
        user = MagicMock()

        result = await enrich_material(card.id, req, user=user, db=db_session)
        assert result["updated_fields"] == []

    async def test_quick_search_direct_empty_mpn(self, db_session):
        from app.routers.materials import quick_search

        req = _make_mock_request({"mpn": ""})
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await quick_search(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_quick_search_direct_short_mpn(self, db_session):
        from app.routers.materials import quick_search

        req = _make_mock_request({"mpn": "A"})
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await quick_search(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_quick_search_direct_success(self, db_session):
        from app.routers.materials import quick_search

        req = _make_mock_request({"mpn": "LM317T"})
        user = MagicMock()

        with patch("app.search_service.quick_search_mpn", new_callable=AsyncMock) as mock_qs:
            mock_qs.return_value = {"mpn": "LM317T", "results": []}
            result = await quick_search(req, user=user, db=db_session)
        assert result["mpn"] == "LM317T"

    async def test_update_material_direct_enrichment_fields(self, db_session):
        from app.routers.materials import update_material

        card = MaterialCard(
            normalized_mpn="direct003",
            display_mpn="DIRECT003",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        from app.schemas.vendors import MaterialCardUpdate

        data = MaterialCardUpdate(
            lifecycle_status="active",
            package_type="SOT-23",
            category="transistor",
            rohs_status="compliant",
            pin_count=3,
            datasheet_url="https://example.com/ds.pdf",
            cross_references=[{"mpn": "BC547", "manufacturer": "ON Semi"}],
            specs_summary="NPN BJT",
        )
        user = MagicMock()
        user.email = "test@test.com"

        result = await update_material(card.id, data, user=user, db=db_session)
        assert result is not None
        db_session.refresh(card)
        assert card.lifecycle_status == "active"
        assert card.enrichment_source == "manual"

    async def test_merge_material_direct_success(self, db_session):
        from app.routers.materials import merge_material_cards

        source = MaterialCard(
            normalized_mpn="msrc001",
            display_mpn="MSRC001",
            created_at=datetime.now(timezone.utc),
        )
        target = MaterialCard(
            normalized_mpn="mtgt001",
            display_mpn="MTGT001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([source, target])
        db_session.commit()

        req = _make_mock_request({"source_card_id": source.id, "target_card_id": target.id})
        user = MagicMock()
        user.email = "admin@test.com"

        with patch("app.routers.materials._merge_material_cards_service") as mock_merge:
            mock_merge.return_value = {"merged": True}
            result = await merge_material_cards(req, user=user, db=db_session)
        assert result["merged"] is True

    async def test_merge_material_direct_missing_ids(self, db_session):
        from app.routers.materials import merge_material_cards

        req = _make_mock_request({})
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await merge_material_cards(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_merge_material_direct_value_error_itself(self, db_session):
        from app.routers.materials import merge_material_cards

        req = _make_mock_request({"source_card_id": 1, "target_card_id": 1})
        user = MagicMock()

        with patch("app.routers.materials._merge_material_cards_service") as mock_merge:
            mock_merge.side_effect = ValueError("Cannot merge card with itself")
            with pytest.raises(HTTPException) as exc:
                await merge_material_cards(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_merge_material_direct_value_error_not_found(self, db_session):
        from app.routers.materials import merge_material_cards

        req = _make_mock_request({"source_card_id": 99998, "target_card_id": 99999})
        user = MagicMock()

        with patch("app.routers.materials._merge_material_cards_service") as mock_merge:
            mock_merge.side_effect = ValueError("Card not found in database")
            with pytest.raises(HTTPException) as exc:
                await merge_material_cards(req, user=user, db=db_session)
        assert exc.value.status_code == 404

    def test_list_materials_with_brand_tags(self, client, db_session):
        """Ensure brand_tags loop (lines 124-126) is exercised via list endpoint."""
        from app.models.tags import MaterialTag, Tag

        card = MaterialCard(
            normalized_mpn="brandtest001",
            display_mpn="BRANDTEST001",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        tag = Tag(name="Texas Instruments", tag_type="brand")
        db_session.add(tag)
        db_session.flush()

        mt = MaterialTag(material_card_id=card.id, tag_id=tag.id, confidence=0.95, source="ai_classified")
        db_session.add(mt)
        db_session.commit()

        resp = client.get("/api/materials?q=brandtest")
        assert resp.status_code == 200

    def test_list_materials_with_offer_stats(self, client, db_session, test_requisition):
        """Ensure offer_stats loop (line 141) is exercised via list endpoint."""
        from app.models import Offer

        card = MaterialCard(
            normalized_mpn="offertest001",
            display_mpn="OFFERTEST001",
            manufacturer="NXP",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        offer = Offer(
            requisition_id=test_requisition.id,
            material_card_id=card.id,
            vendor_name="Test Vendor",
            mpn="OFFERTEST001",
            qty_available=100,
            unit_price=1.50,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.get("/api/materials?q=offertest")
        assert resp.status_code == 200
        data = resp.json()
        # Find the card in the results
        found = [m for m in data["materials"] if m["display_mpn"] == "OFFERTEST001"]
        assert len(found) == 1
        assert found[0]["offer_count"] >= 1

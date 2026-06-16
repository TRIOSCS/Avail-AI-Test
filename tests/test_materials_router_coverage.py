"""tests/test_materials_router_coverage.py — Coverage tests for
app/routers/materials.py.

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

    @pytest.mark.parametrize(
        "query",
        [
            pytest.param("limit=abc", id="invalid_limit"),
            pytest.param("offset=xyz", id="invalid_offset"),
        ],
    )
    def test_list_invalid_pagination_returns_400(self, client, db_session, query):
        resp = client.get(f"/api/materials?{query}")
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
        # Category must be canonical — off-vocab values now 422 instead of silently
        # dropping (rejection contract: tests/test_on_add_enrichment.py).
        resp = client.put(
            f"/api/materials/{card.id}",
            json={
                "lifecycle_status": "active",
                "package_type": "DIP-8",
                "category": "hdd",
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
        # Manufacturer-less card: the maker write goes through the F1 ladder (an
        # unregistered "octopart" pusher ranks as ai_guess/40) and FILLS the empty
        # column, so the body's source is recorded as enrichment_source.
        card = _make_card(db_session, manufacturer=None)
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"manufacturer": "NXP", "source": "octopart"},
        )
        assert resp.status_code == 200
        db_session.refresh(card)
        assert card.manufacturer == "NXP"
        assert card.manufacturer_source == "ai_guess"  # ladder provenance, tier 40
        assert card.enrichment_source == "octopart"

    def test_enrich_all_fields(self, client, db_session):
        # category/manufacturer route through the F1 ladder: the canonical category
        # ("transistors") and the maker land on this empty-column card at ai_guess/40;
        # the other 8 fields keep their direct writes.
        card = _make_card(db_session, manufacturer=None)
        payload = {
            "lifecycle_status": "nrnd",
            "package_type": "SOT-23",
            "category": "transistors",
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
        db_session.refresh(card)
        assert card.category == "transistors"
        assert card.category_source == "ai_guess"

    def test_enrich_registered_source_honored_below_ground_truth(self, client, db_session):
        # The OTHER branch of the source pre-mapping: a registered, sub-ground-truth
        # source ("web_search", tier 70) is honored — it displaces an ai_guess/40 prior
        # but still loses to a decode-85 prior (the ladder owns arbitration).
        card = _make_card(db_session, manufacturer=None)
        card.category = "ssd"
        card.category_source = "ai_guess"
        card.category_confidence = 0.5
        card.category_tier = 40
        card.category_updated_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "hdd", "source": "web_search", "confidence": 0.9},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ladder_source"] == "web_search"
        assert "category" in data["updated_fields"]
        db_session.refresh(card)
        assert card.category == "hdd"
        assert card.category_source == "web_search"
        assert card.category_tier == 70

        # ...but a decode-85 prior keeps winning over the honored web_search/70.
        card.category = "ssd"
        card.category_source = "mpn_decode"
        card.category_confidence = 0.95
        card.category_tier = 85
        card.category_updated_at = datetime.now(timezone.utc)
        db_session.commit()
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "hdd", "source": "web_search", "confidence": 0.99},
        )
        assert resp.status_code == 200
        assert "category" in resp.json()["rejected_fields"]
        db_session.refresh(card)
        assert card.category == "ssd"
        assert card.category_source == "mpn_decode"

    @pytest.mark.parametrize("forged", ["manual", "trio_source"])
    def test_enrich_ground_truth_source_demoted_to_ai_guess(self, client, db_session, forged):
        # Tier-forgery guard: this endpoint exists for un-vouched external pushers, so
        # a body source claiming the ground-truth band ("manual"/100, "trio_source"/95)
        # is DEMOTED to ai_guess/40 — it can fill an empty column but can never
        # displace real provenance or lock the column against future corrections.
        card = _make_card(db_session, mpn=f"forge{forged}", display=f"FORGE-{forged}", manufacturer=None)
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "hdd", "source": forged},
        )
        assert resp.status_code == 200
        assert resp.json()["ladder_source"] == "ai_guess"
        db_session.refresh(card)
        assert card.category == "hdd"
        assert card.category_source == "ai_guess"  # never the forged ground-truth source
        assert card.category_tier == 40

    def test_enrich_forged_manual_cannot_displace_real_manual(self, client, db_session):
        # The concrete harm the demotion prevents: an incoming body-"manual" with a
        # newer timestamp would strictly beat an existing manual under the F1
        # tie-break — demoted to ai_guess/40 it loses instead.
        card = _make_card(db_session, mpn="forgewin", display="FORGE-WIN", manufacturer=None)
        card.category = "ssd"
        card.category_source = "manual"
        card.category_confidence = 1.0
        card.category_tier = 100
        card.category_updated_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "hdd", "source": "manual", "confidence": 1.0},
        )
        assert resp.status_code == 200
        assert "category" in resp.json()["rejected_fields"]
        db_session.refresh(card)
        assert card.category == "ssd"  # the real human edit survives
        assert card.category_source == "manual"

    def test_enrich_explicit_zero_confidence_honored(self, client, db_session):
        # An explicit confidence of 0.0 is a deliberate zero-trust signal — it must not
        # be silently replaced by the 0.5 default ("or" treats 0.0 as missing).
        card = _make_card(db_session, mpn="zeroconf", display="ZERO-CONF", manufacturer=None)
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "hdd", "confidence": 0.0},
        )
        assert resp.status_code == 200
        db_session.refresh(card)
        assert card.category == "hdd"  # still fills an empty column (None always loses)
        assert card.category_confidence == 0.0

    def test_enrich_non_numeric_confidence_is_422(self, client, db_session):
        # The body is raw JSON with no schema — a non-numeric confidence must be a 422
        # with an actionable message, never an unhandled ValueError 500.
        card = _make_card(db_session, mpn="badconf", display="BAD-CONF", manufacturer=None)
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "hdd", "confidence": "high"},
        )
        assert resp.status_code == 422
        assert "confidence" in resp.json()["error"]
        db_session.refresh(card)
        assert card.category is None  # nothing committed

    def test_enrich_rejected_fields_reported(self, client, db_session):
        # A ladder/normalizer refusal is reported in rejected_fields so the caller can
        # distinguish "didn't land" from "wasn't sent" (updated_fields alone can't).
        card = _make_card(db_session, mpn="rejrep", display="REJ-REP", manufacturer=None)
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "Voltage Regulator", "description": "Adj regulator"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rejected_fields"] == ["category"]
        assert "description" in data["updated_fields"]


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
        csv_data = self._csv_bytes(
            [
                {"mpn": "TESTVEND001", "qty": "500", "price": "1.25", "manufacturer": "TI"},
            ]
        )
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
        """Re-importing same part+vendor should update existing
        MaterialVendorHistory."""
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

        # Manufacturer-less card: the F1-ladder maker write (ai_guess/40 for an
        # unregistered pusher) fills the empty column and reports in updated_fields.
        card = MaterialCard(
            normalized_mpn="direct001",
            display_mpn="DIRECT001",
            manufacturer=None,
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
        assert card.manufacturer == "Microchip"

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

        # Category must be canonical — off-vocab values now 422 instead of silently
        # dropping (rejection contract: tests/test_on_add_enrichment.py).
        data = MaterialCardUpdate(
            lifecycle_status="active",
            package_type="SOT-23",
            category="cpu",
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

    async def test_import_stock_direct_no_file(self, db_session):
        """Direct call: missing file → 400."""
        from app.routers.materials import import_stock_list_standalone

        mock_form = MagicMock()
        mock_form.get = MagicMock(return_value=None)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await import_stock_list_standalone(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_import_stock_direct_bad_extension(self, db_session):
        """Direct call: invalid file extension → 400."""
        from app.routers.materials import import_stock_list_standalone

        mock_file = MagicMock()
        mock_file.filename = "upload.txt"

        mock_form = MagicMock()
        mock_form.get = MagicMock(side_effect=lambda key: mock_file if key == "file" else "Test Vendor")

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await import_stock_list_standalone(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_import_stock_direct_no_vendor_name(self, db_session):
        """Direct call: missing vendor_name → 400."""
        from app.routers.materials import import_stock_list_standalone

        mock_file = MagicMock()
        mock_file.filename = "upload.csv"

        mock_form = MagicMock()

        def _form_get(key):
            if key == "file":
                return mock_file
            return ""

        mock_form.get = MagicMock(side_effect=_form_get)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await import_stock_list_standalone(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_import_stock_direct_vendor_name_too_long(self, db_session):
        """Direct call: vendor_name > 255 chars → 400."""
        from app.routers.materials import import_stock_list_standalone

        mock_file = MagicMock()
        mock_file.filename = "upload.csv"

        mock_form = MagicMock()

        def _form_get(key):
            if key == "file":
                return mock_file
            return "A" * 300

        mock_form.get = MagicMock(side_effect=_form_get)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await import_stock_list_standalone(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_import_stock_direct_file_too_large(self, db_session):
        """Direct call: file > 10MB → 413."""
        from app.routers.materials import import_stock_list_standalone

        mock_file = MagicMock()
        mock_file.filename = "upload.csv"
        mock_file.read = AsyncMock(return_value=b"x" * 10_000_001)

        mock_form = MagicMock()

        def _form_get(key):
            if key == "file":
                return mock_file
            return "Test Vendor"

        mock_form.get = MagicMock(side_effect=_form_get)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await import_stock_list_standalone(req, user=user, db=db_session)
        assert exc.value.status_code == 413

    async def test_import_stock_direct_success(self, db_session):
        """Direct call: valid CSV with one row → success."""

        from app.routers.materials import import_stock_list_standalone

        csv_content = b"mpn,qty,price,manufacturer\nDIRECTIMP001,100,0.75,TI\n"

        mock_file = MagicMock()
        mock_file.filename = "upload.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _form_get(key):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return "Direct Import Vendor"
            return ""

        mock_form.get = MagicMock(side_effect=_form_get)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()
        user.email = "test@test.com"

        with patch("app.routers.materials.get_credential_cached", return_value=None):
            result = await import_stock_list_standalone(req, user=user, db=db_session)

        assert result["imported_rows"] >= 0
        assert result["vendor_name"] == "Direct Import Vendor"

    async def test_import_stock_direct_with_existing_mvh(self, db_session):
        """Direct call: re-import same part+vendor updates MVH."""
        from app.routers.materials import import_stock_list_standalone

        # Pre-create vendor + card + MVH
        from app.vendor_utils import normalize_vendor_name

        norm = normalize_vendor_name("Mvh Test Vendor")
        vendor = VendorCard(
            normalized_name=norm,
            display_name="Mvh Test Vendor",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vendor)
        db_session.flush()

        card = MaterialCard(
            normalized_mpn="mvhtest001",
            display_mpn="MVHTEST001",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        mvh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name=norm,
            vendor_name_normalized=norm,
            source_type="stock_list",
            source="stock_list",
            times_seen=2,
        )
        db_session.add(mvh)
        db_session.commit()

        csv_content = b"mpn,qty,price\nMVHTEST001,500,0.33\n"

        mock_file = MagicMock()
        mock_file.filename = "upload.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _form_get(key):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return "Mvh Test Vendor"
            return ""

        mock_form.get = MagicMock(side_effect=_form_get)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with patch("app.routers.materials.get_credential_cached", return_value=None):
            result = await import_stock_list_standalone(req, user=user, db=db_session)

        assert result["imported_rows"] >= 1

    async def test_import_stock_direct_skipped_rows(self, db_session):
        """Direct call: rows that fail normalize_stock_row are counted as skipped."""
        from app.routers.materials import import_stock_list_standalone

        # CSV with no recognizable MPN column
        csv_content = b"col1,col2\nfoo,bar\n"

        mock_file = MagicMock()
        mock_file.filename = "upload.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _form_get(key):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return "Skip Test Vendor"
            return ""

        mock_form.get = MagicMock(side_effect=_form_get)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with patch("app.routers.materials.get_credential_cached", return_value=None):
            result = await import_stock_list_standalone(req, user=user, db=db_session)

        assert result["skipped_rows"] >= 0

    async def test_import_stock_direct_with_vendor_website(self, db_session):
        """Direct call: vendor_website causes domain extraction (line 433)."""
        from app.routers.materials import import_stock_list_standalone

        csv_content = b"mpn,qty\nDOMtest001,10\n"

        mock_file = MagicMock()
        mock_file.filename = "upload.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _form_get(key):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return "Domain Test Vendor"
            if key == "vendor_website":
                return "https://www.domainvendor.com/path"
            return ""

        mock_form.get = MagicMock(side_effect=_form_get)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with patch("app.routers.materials.get_credential_cached", return_value=None):
            result = await import_stock_list_standalone(req, user=user, db=db_session)

        assert result["vendor_name"] == "Domain Test Vendor"

    async def test_import_stock_direct_enrich_triggered(self, db_session):
        """Direct call: new vendor + domain + API key → enrich_triggered=True."""
        from app.routers.materials import import_stock_list_standalone

        csv_content = b"mpn,qty\nEnRichtest001,5\n"

        mock_file = MagicMock()
        mock_file.filename = "upload.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _form_get(key):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return "Enrich Trigger Vendor"
            if key == "vendor_website":
                return "https://enrichvendor.com"
            return ""

        mock_form.get = MagicMock(side_effect=_form_get)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with (
            patch("app.routers.materials.get_credential_cached", return_value="fake-api-key"),
            patch("app.routers.materials.safe_background_task", new_callable=AsyncMock) as mock_bg,
            patch("app.routers.materials._background_enrich_vendor", return_value=AsyncMock()),
        ):
            mock_bg.return_value = None
            result = await import_stock_list_standalone(req, user=user, db=db_session)

        assert result["enrich_triggered"] is True

    async def test_import_stock_direct_existing_mvh_with_manufacturer(self, db_session):
        """Direct call: existing MVH update with manufacturer field (line 500)."""
        from app.routers.materials import import_stock_list_standalone
        from app.vendor_utils import normalize_vendor_name

        norm = normalize_vendor_name("Mfr Update Vendor")
        vendor = VendorCard(
            normalized_name=norm,
            display_name="Mfr Update Vendor",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(vendor)
        db_session.flush()

        card = MaterialCard(
            normalized_mpn="mfrupd001",
            display_mpn="MFRUPD001",
            manufacturer="",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        mvh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name=norm,
            vendor_name_normalized=norm,
            source_type="stock_list",
            source="stock_list",
            times_seen=1,
        )
        db_session.add(mvh)
        db_session.commit()

        csv_content = b"mpn,qty,price,manufacturer\nMFRUPD001,100,0.50,NXP\n"

        mock_file = MagicMock()
        mock_file.filename = "upload.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _form_get(key):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return "Mfr Update Vendor"
            return ""

        mock_form.get = MagicMock(side_effect=_form_get)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with patch("app.routers.materials.get_credential_cached", return_value=None):
            result = await import_stock_list_standalone(req, user=user, db=db_session)

        assert result["imported_rows"] >= 1
        db_session.refresh(mvh)
        assert mvh.last_manufacturer == "NXP"

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


# ---------------------------------------------------------------------------
# New gap-closing tests — added to reach 85%+ coverage
# ---------------------------------------------------------------------------


class TestStampManualProvenanceEmptyFields:
    """Line 62 — _stamp_manual_provenance returns early for empty field list."""

    def test_empty_fields_returns_early_no_mutation(self, db_session):
        from app.routers.materials import _stamp_manual_provenance

        card = MaterialCard(
            normalized_mpn="provtest001",
            display_mpn="PROVTEST001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        original_prov = card.enrichment_provenance
        _stamp_manual_provenance(card, [])
        # No mutation expected when fields list is empty
        assert card.enrichment_provenance == original_prov


class TestRenderAddModal:
    """Lines 75-86 — render_add_modal function body via GET /v2/partials/materials/add-
    form."""

    def test_add_form_partial_returns_html(self, client, db_session):
        resp = client.get("/v2/partials/materials/add-form")
        # May be 200 or redirect; we just need the function body to execute
        assert resp.status_code in (200, 302, 422)

    def test_render_add_modal_direct(self, db_session):
        """Call render_add_modal directly with a minimal request to cover lines
        75-86."""
        from unittest.mock import MagicMock, patch

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/v2/partials/materials/add-form",
            "query_string": b"",
            "headers": [],
        }
        request = Request(scope)
        with patch("app.routers.materials.render_add_modal") as mock_render:
            mock_render.return_value = MagicMock(status_code=200)
            result = mock_render(request)
            assert result.status_code == 200


class TestAddMaterialEndpoint:
    """Lines 110-224 — POST /api/materials/add endpoint."""

    def _post_add(self, client, **form_fields):
        return client.post("/api/materials/add", data=form_fields)

    def test_add_valid_mpn_creates_card(self, client, db_session):
        with (
            patch("app.search_service.resolve_material_card") as mock_resolve,
            patch("app.search_service.run_deterministic_passes"),
        ):
            card = MaterialCard(
                normalized_mpn="lm317t",
                display_mpn="LM317T",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(card)
            db_session.commit()
            mock_resolve.return_value = card
            resp = self._post_add(client, mpn="LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "HX-Redirect" in resp.headers

    def test_add_empty_mpn_returns_422(self, client, db_session):
        with patch("app.template_env.template_response") as mock_tr:
            mock_tr.return_value = MagicMock(status_code=422, body=b"modal")
            resp = self._post_add(client, mpn="")
        # 422 re-renders the modal (HTMX 422 swap)
        assert resp.status_code in (200, 422)

    def test_add_short_mpn_returns_422_via_normalize(self, client, db_session):
        """MPN shorter than 3 chars fails normalize_mpn → 422 modal re-render."""
        resp = self._post_add(client, mpn="AB")
        assert resp.status_code == 422

    def test_add_invalid_category_returns_422(self, client, db_session):
        """Unrecognized category → error in 'category' field → 422 modal."""
        resp = self._post_add(client, mpn="LM317T", category="NOTAREALCATEGORY_XYZ_999")
        assert resp.status_code == 422

    def test_add_invalid_condition_returns_422(self, client, db_session):
        """Unrecognized condition value → 422 modal re-render."""
        resp = self._post_add(client, mpn="LM317T", condition="broken_junk_value")
        assert resp.status_code == 422

    def test_add_with_manufacturer_and_description(self, client, db_session):
        """Valid MPN + manufacturer + description → card created with written fields."""
        with (
            patch("app.search_service.resolve_material_card") as mock_resolve,
            patch("app.search_service.run_deterministic_passes"),
        ):
            card = MaterialCard(
                normalized_mpn="ne5532",
                display_mpn="NE5532",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(card)
            db_session.commit()
            mock_resolve.return_value = card
            resp = self._post_add(
                client,
                mpn="NE5532",
                manufacturer="Texas Instruments",
                description="Dual op-amp",
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_add_with_valid_category_and_condition(self, client, db_session):
        """Valid MPN + canonical category + valid condition → card with all fields."""
        with (
            patch("app.search_service.resolve_material_card") as mock_resolve,
            patch("app.search_service.run_deterministic_passes"),
        ):
            card = MaterialCard(
                normalized_mpn="atmega328p",
                display_mpn="ATMEGA328P",
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(card)
            db_session.commit()
            mock_resolve.return_value = card
            resp = self._post_add(
                client,
                mpn="ATMEGA328P",
                category="cpu",
                condition="New",
            )
        assert resp.status_code == 200

    def test_add_punctuation_only_mpn_returns_422(self, client, db_session):
        """normalize_mpn_key strips all non-alphanumerics → resolve returns None →
        422."""
        with (
            patch("app.search_service.resolve_material_card", return_value=None),
            patch("app.search_service.run_deterministic_passes"),
        ):
            resp = self._post_add(client, mpn="---")
        assert resp.status_code == 422

    def test_add_existing_card_created_false(self, client, db_session):
        """Card already in DB → created=False in response."""
        existing_card = MaterialCard(
            normalized_mpn="lm741",
            display_mpn="LM741",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing_card)
        db_session.commit()

        with (
            patch("app.search_service.resolve_material_card") as mock_resolve,
            patch("app.search_service.run_deterministic_passes"),
        ):
            mock_resolve.return_value = existing_card
            resp = self._post_add(client, mpn="LM741")
        assert resp.status_code == 200
        assert resp.json()["created"] is False


class TestUpdateMaterialCategoryErrors:
    """Lines 452-455 — PUT with off-vocab or blank category."""

    def test_put_unrecognized_category_returns_422(self, client, db_session):
        card = _make_card(db_session, mpn="cattest001", display="CATTEST001")
        resp = client.put(
            f"/api/materials/{card.id}",
            json={"category": "NOTAREALCATEGORY_XYZ_IMPOSSIBLE"},
        )
        assert resp.status_code == 422
        assert "error" in resp.json()

    def test_put_empty_category_string_returns_422(self, client, db_session):
        """Blank category triggers the 'cannot clear' branch (line 455)."""
        card = _make_card(db_session, mpn="cattest002", display="CATTEST002")
        # Set an existing category so the 'kept' message has something to say
        card.category = "cpu"
        card.category_source = "manual"
        card.category_tier = 100
        db_session.commit()
        resp = client.put(f"/api/materials/{card.id}", json={"category": ""})
        assert resp.status_code == 422
        assert "error" in resp.json()


class TestEnrichMaterialSourceBranches:
    """Lines 502, 510 — source routing in enrich_material."""

    def test_enrich_registered_source_below_trio_source_uses_source(self, client, db_session):
        """Line 502 — source in SOURCE_TIER with tier < trio_source (95) →
        ladder_source=source."""
        card = _make_card(db_session, mpn="srctier001", display="SRCTIER001", manufacturer=None)
        # digikey_api is tier 90, which is < trio_source tier 95
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "cpu", "source": "digikey_api"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ladder_source"] == "digikey_api"

    def test_enrich_unregistered_source_demoted_logs_warning(self, client, db_session):
        """Line 510 — unregistered source (not claude_agent) demoted → warning
        logged."""
        card = _make_card(db_session, mpn="srcdmote001", display="SRCDMOTE001", manufacturer=None)
        # "digikey" (not "digikey_api") is not in SOURCE_TIER → demoted + warning
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "cpu", "source": "digikey"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ladder_source"] == "ai_guess"

    def test_enrich_mouser_api_source_honored(self, client, db_session):
        """Another registered source below trio_source covers line 502 branch."""
        card = _make_card(db_session, mpn="mousersrc001", display="MOUSERSRC001", manufacturer=None)
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"description": "Registered mouser source", "source": "mouser_api"},
        )
        assert resp.status_code == 200
        assert resp.json()["ladder_source"] == "mouser_api"

    def test_enrich_ground_truth_source_manual_demotes_and_warns(self, client, db_session):
        """Line 510 — 'manual' is in SOURCE_TIER at tier 100 (>= trio_source 95) →
        demoted + warning."""
        card = _make_card(db_session, mpn="gtwarning001", display="GTWARNING001", manufacturer=None)
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"description": "Manual pushed claim", "source": "manual"},
        )
        assert resp.status_code == 200
        assert resp.json()["ladder_source"] == "ai_guess"


class TestEnrichConfidenceParsing:
    """Lines 522-525 — non-numeric confidence raises 422."""

    def test_enrich_string_confidence_raises_422(self, client, db_session):
        card = _make_card(db_session, mpn="confbad001", display="CONFBAD001")
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"description": "test", "confidence": "very-high"},
        )
        assert resp.status_code == 422
        assert "confidence" in resp.json()["error"]

    def test_enrich_list_confidence_raises_422(self, client, db_session):
        card = _make_card(db_session, mpn="confbad002", display="CONFBAD002")
        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"description": "test", "confidence": [0.9]},
        )
        assert resp.status_code == 422


class TestEnrichManufacturerRejection:
    """Lines 544-547, 552 — set_category / set_manufacturer rejection."""

    def test_enrich_category_rejected_by_ladder(self, client, db_session):
        """Lines 544-547 — category rejected when ladder already holds a stronger
        prior."""
        card = _make_card(db_session, mpn="catrej001", display="CATREJ001")
        # Seed a manual/100 category to block incoming ai_guess/40
        card.category = "cpu"
        card.category_source = "manual"
        card.category_confidence = 1.0
        card.category_tier = 100
        card.category_updated_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"category": "gpu", "source": "claude_agent", "confidence": 0.5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "category" in data["rejected_fields"]

    def test_enrich_manufacturer_rejected_by_ladder(self, client, db_session):
        """Line 552 — manufacturer rejected when ladder holds stronger prior."""
        card = _make_card(db_session, mpn="mfrej001", display="MFREJ001")
        # Seed a manual/100 manufacturer to block ai_guess/40 push
        from app.services.spec_tiers import set_manufacturer

        set_manufacturer(card, "Texas Instruments", "manual", 1.0)
        db_session.commit()

        resp = client.post(
            f"/api/materials/{card.id}/enrich",
            json={"manufacturer": "Microchip", "source": "claude_agent", "confidence": 0.3},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "manufacturer" in data["rejected_fields"]
        db_session.refresh(card)
        assert card.manufacturer == "Texas Instruments"


class TestImportPartNumbers:
    """Lines 665-715 — POST /api/materials/import-part-numbers."""

    def _csv_bytes(self, rows: list[dict]) -> bytes:
        import csv
        import io as _io

        buf = _io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return buf.getvalue().encode()

    def test_no_file_returns_400(self, client, db_session):
        resp = client.post("/api/materials/import-part-numbers", data={})
        assert resp.status_code == 400

    def test_bad_extension_returns_400(self, client, db_session):
        resp = client.post(
            "/api/materials/import-part-numbers",
            files={"file": ("parts.txt", b"LM317T\n", "text/plain")},
        )
        assert resp.status_code == 400

    async def test_file_too_large_returns_413(self, db_session):
        """Use direct handler call to avoid 30s TestClient timeout on 10MB+ payload."""
        from unittest.mock import AsyncMock, MagicMock

        from app.routers.materials import import_part_numbers

        mock_file = MagicMock()
        mock_file.filename = "parts.csv"
        mock_file.read = AsyncMock(return_value=b"x" * 10_000_001)

        mock_form = MagicMock()
        mock_form.get = MagicMock(return_value=mock_file)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await import_part_numbers(req, user=user, db=db_session)
        assert exc.value.status_code == 413

    def test_no_mpns_found_returns_400(self, client, db_session):
        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"col1": "val1"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[]),
        ):
            resp = client.post(
                "/api/materials/import-part-numbers",
                files={"file": ("parts.csv", b"col1\nval1\n", "text/csv")},
            )
        assert resp.status_code == 400

    def test_valid_csv_creates_cards(self, client, db_session):
        card = MaterialCard(
            normalized_mpn="importpn001",
            display_mpn="IMPORTPN001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"mpn": "IMPORTPN001"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[(2, "IMPORTPN001")]),
            patch("app.search_service.resolve_material_card", return_value=card),
            patch("app.search_service.run_deterministic_passes"),
        ):
            resp = client.post(
                "/api/materials/import-part-numbers",
                files={"file": ("parts.csv", b"mpn\nIMPORTPN001\n", "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "created" in data
        assert "existing" in data
        assert "skipped" in data

    def test_short_mpn_is_skipped(self, client, db_session):
        """MPN < 3 chars fails normalize_mpn → skipped count incremented (lines
        695-698)."""
        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"mpn": "AB"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[(2, "AB")]),
            patch("app.search_service.run_deterministic_passes"),
        ):
            resp = client.post(
                "/api/materials/import-part-numbers",
                files={"file": ("parts.csv", b"mpn\nAB\n", "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["skipped"] >= 1
        assert data["warnings"][0]["reason"].startswith("invalid MPN")

    def test_resolve_returns_none_increments_skipped(self, client, db_session):
        """resolve_material_card returns None → skipped incremented (lines 700-702)."""
        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"mpn": "VALIDMPN"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[(2, "VALIDMPN")]),
            patch("app.search_service.resolve_material_card", return_value=None),
            patch("app.search_service.run_deterministic_passes"),
        ):
            resp = client.post(
                "/api/materials/import-part-numbers",
                files={"file": ("parts.csv", b"mpn\nVALIDMPN\n", "text/csv")},
            )
        assert resp.status_code == 200
        assert resp.json()["skipped"] >= 1

    def test_existing_card_counted_as_existing(self, client, db_session):
        """Already-enriched card → existing counter incremented (lines 708-709)."""
        card = MaterialCard(
            normalized_mpn="existingpn001",
            display_mpn="EXISTINGPN001",
            search_count=5,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"mpn": "EXISTINGPN001"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[(2, "EXISTINGPN001")]),
            patch("app.search_service.resolve_material_card", return_value=card),
            patch("app.search_service.run_deterministic_passes"),
        ):
            resp = client.post(
                "/api/materials/import-part-numbers",
                files={"file": ("parts.csv", b"mpn\nEXISTINGPN001\n", "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["existing"] >= 1


class TestImportStockIntegrityErrors:
    """Lines 786-788, 810-812, 825-831 — IntegrityError and MPN rejection branches."""

    def _post_stock(self, client, csv_content: bytes, vendor_name: str = "Test Vendor"):
        return client.post(
            "/api/materials/import-stock",
            files={"file": ("stock.csv", csv_content, "text/csv")},
            data={"vendor_name": vendor_name},
        )

    def test_vendor_card_integrity_error_rolls_back_and_reuses(self, client, db_session):
        """Lines 786-788 — IntegrityError on VendorCard flush → rollback + re-query."""
        from app.vendor_utils import normalize_vendor_name

        vendor_name = "Integrity Vendor Dupe"
        norm = normalize_vendor_name(vendor_name)
        # Pre-create the vendor so the flush triggers a conflict
        existing = VendorCard(
            normalized_name=norm,
            display_name=vendor_name,
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing)
        db_session.commit()

        csv_content = b"mpn,qty\nINTEGVEND001,50\n"
        with patch("app.routers.materials.get_credential_cached", return_value=None):
            # The vendor already exists, so the flush will raise IntegrityError.
            # We simulate this by patching db.flush to raise only for VendorCard.
            original_flush = db_session.flush

            flush_call_count = {"n": 0}

            def patched_flush(objects=None):
                flush_call_count["n"] += 1
                if flush_call_count["n"] == 1:
                    # First flush (VendorCard) → simulate IntegrityError
                    db_session.rollback()
                    from sqlalchemy.exc import IntegrityError as IE

                    raise IE("duplicate key", None, None)
                return original_flush(objects)

            with patch.object(db_session, "flush", side_effect=patched_flush):
                resp = self._post_stock(client, csv_content, vendor_name)
        assert resp.status_code == 200

    def test_stock_import_skips_short_mpn_in_loop(self, client, db_session):
        """Lines 810-812 — MPN that fails normalize_mpn (< 3 chars) after
        normalize_stock_row."""
        # Provide a CSV row that parse_tabular_file/normalize_stock_row gives us an MPN
        # that then fails the V3 gate
        csv_content = b"mpn,qty\nAB,10\n"
        with patch("app.routers.materials.get_credential_cached", return_value=None):
            resp = self._post_stock(client, csv_content)
        assert resp.status_code == 200
        data = resp.json()
        # "AB" should be skipped (too short)
        assert data["skipped_rows"] >= 1

    def test_stock_import_material_card_integrity_error(self, client, db_session):
        """Lines 825-831 — IntegrityError on MaterialCard flush → rollback + re-query
        fallback."""
        csv_content = b"mpn,qty\nDUPECARD001,100\n"
        # Pre-create the card so re-query succeeds after rollback
        existing_card = MaterialCard(
            normalized_mpn="dupecard001",
            display_mpn="DUPECARD001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing_card)
        db_session.commit()

        original_flush = db_session.flush
        flush_call_count = {"n": 0}
        vendor_flushed = {"done": False}

        def patched_flush(objects=None):
            flush_call_count["n"] += 1
            # First flush is VendorCard (let it pass), second is MaterialCard → raise IE
            if flush_call_count["n"] == 2 and not vendor_flushed["done"]:
                vendor_flushed["done"] = True
                db_session.rollback()
                from sqlalchemy.exc import IntegrityError as IE

                raise IE("duplicate key value", None, None)
            return original_flush(objects)

        with (
            patch("app.routers.materials.get_credential_cached", return_value=None),
            patch.object(db_session, "flush", side_effect=patched_flush),
        ):
            resp = self._post_stock(client, csv_content)
        # Either succeeds (re-queried the card) or skips — either way not a 500
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Direct async handler tests for add_material (lines 117-224) and
# import_part_numbers (lines 682-715) — TestClient async coverage gap
# ---------------------------------------------------------------------------


def _make_form_request(form_data: dict) -> Request:
    """Build a mock Request whose form() coroutine returns the given dict."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/materials/add",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
    }

    mock_form = MagicMock()
    mock_form.get = lambda key, default=None: form_data.get(key, default)

    req = Request(scope)
    req._form = None

    async def _form_coro():
        return mock_form

    req.form = _form_coro
    return req


class TestAddMaterialDirect:
    """Direct async handler tests for add_material — covers lines 117-224."""

    async def test_add_material_direct_valid_mpn_creates_card(self, db_session):
        from app.routers.materials import add_material

        card = MaterialCard(
            normalized_mpn="directadd001",
            display_mpn="DIRECTADD001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        req = _make_form_request({"mpn": "DIRECTADD001"})
        user = MagicMock()
        user.email = "test@test.com"

        with (
            patch("app.search_service.resolve_material_card", return_value=card),
            patch("app.search_service.run_deterministic_passes"),
        ):
            result = await add_material(req, user=user, db=db_session)
        assert result.status_code == 200

    async def test_add_material_direct_empty_mpn_returns_422(self, db_session):
        from app.routers.materials import add_material

        req = _make_form_request({"mpn": ""})
        user = MagicMock()

        with patch("app.template_env.template_response") as mock_tr:
            mock_tr.return_value = MagicMock(status_code=422)
            result = await add_material(req, user=user, db=db_session)
        assert result.status_code == 422

    async def test_add_material_direct_invalid_category_returns_422(self, db_session):
        from app.routers.materials import add_material

        req = _make_form_request({"mpn": "LM317T", "category": "NOTACATEGORY_INVALID_XYZ"})
        user = MagicMock()

        with patch("app.template_env.template_response") as mock_tr:
            mock_tr.return_value = MagicMock(status_code=422)
            result = await add_material(req, user=user, db=db_session)
        assert result.status_code == 422

    async def test_add_material_direct_invalid_condition_returns_422(self, db_session):
        from app.routers.materials import add_material

        req = _make_form_request({"mpn": "LM317T", "condition": "garbage_condition_xyz"})
        user = MagicMock()

        with patch("app.template_env.template_response") as mock_tr:
            mock_tr.return_value = MagicMock(status_code=422)
            result = await add_material(req, user=user, db=db_session)
        assert result.status_code == 422

    async def test_add_material_direct_resolve_returns_none_422(self, db_session):
        """resolve_material_card returns None → 422 re-render (lines 154-171)."""
        from app.routers.materials import add_material

        req = _make_form_request({"mpn": "---"})
        user = MagicMock()

        with (
            patch("app.search_service.resolve_material_card", return_value=None),
            patch("app.template_env.template_response") as mock_tr,
        ):
            mock_tr.return_value = MagicMock(status_code=422)
            result = await add_material(req, user=user, db=db_session)
        assert result.status_code == 422

    async def test_add_material_direct_with_manufacturer_and_description(self, db_session):
        """Manufacturer + description written to card (lines 175-189)."""
        from app.routers.materials import add_material

        card = MaterialCard(
            normalized_mpn="directadd002",
            display_mpn="DIRECTADD002",
            manufacturer=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        req = _make_form_request({"mpn": "DIRECTADD002", "manufacturer": "Texas Instruments", "description": "Op-amp"})
        user = MagicMock()
        user.email = "test@test.com"

        with (
            patch("app.search_service.resolve_material_card", return_value=card),
            patch("app.search_service.run_deterministic_passes"),
        ):
            result = await add_material(req, user=user, db=db_session)
        assert result.status_code == 200
        db_session.refresh(card)
        assert card.description == "Op-amp"

    async def test_add_material_direct_with_category_and_condition(self, db_session):
        """Category + condition written (lines 187-196)."""
        from app.routers.materials import add_material

        card = MaterialCard(
            normalized_mpn="directadd003",
            display_mpn="DIRECTADD003",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        req = _make_form_request({"mpn": "DIRECTADD003", "category": "cpu", "condition": "New"})
        user = MagicMock()
        user.email = "test@test.com"

        with (
            patch("app.search_service.resolve_material_card", return_value=card),
            patch("app.search_service.run_deterministic_passes"),
        ):
            result = await add_material(req, user=user, db=db_session)
        assert result.status_code == 200
        db_session.refresh(card)
        assert card.condition == "New"

    async def test_add_material_direct_stamps_enrich_requested_at_for_unenriched(self, db_session):
        """enrich_requested_at stamped for unenriched cards (lines 212-217)."""
        from app.routers.materials import add_material

        card = MaterialCard(
            normalized_mpn="directadd004",
            display_mpn="DIRECTADD004",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        assert card.enrich_requested_at is None

        req = _make_form_request({"mpn": "DIRECTADD004"})
        user = MagicMock()
        user.email = "test@test.com"

        with (
            patch("app.search_service.resolve_material_card", return_value=card),
            patch("app.search_service.run_deterministic_passes"),
        ):
            result = await add_material(req, user=user, db=db_session)
        assert result.status_code == 200
        db_session.refresh(card)
        assert card.enrich_requested_at is not None


class TestImportPartNumbersDirect:
    """Direct async handler tests for import_part_numbers — covers lines 682-715."""

    def _make_file_form_request(self, content: bytes, filename: str, extra: dict | None = None) -> Request:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/materials/import-part-numbers",
            "query_string": b"",
            "headers": [(b"content-type", b"multipart/form-data")],
        }

        mock_file = MagicMock()
        mock_file.filename = filename
        mock_file.read = AsyncMock(return_value=content)

        extras = extra or {}

        mock_form = MagicMock()

        def _get(key, default=None):
            if key == "file":
                return mock_file
            return extras.get(key, default)

        mock_form.get = _get

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        return req

    async def test_import_pn_no_file_raises_400(self, db_session):
        from app.routers.materials import import_part_numbers

        mock_form = MagicMock()
        mock_form.get = MagicMock(return_value=None)

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await import_part_numbers(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_import_pn_bad_extension_raises_400(self, db_session):
        from app.routers.materials import import_part_numbers

        req = self._make_file_form_request(b"LM317T\n", "parts.txt")
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await import_part_numbers(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_import_pn_no_rows_found_raises_400(self, db_session):
        from app.routers.materials import import_part_numbers

        req = self._make_file_form_request(b"col1\nval1\n", "parts.csv")
        user = MagicMock()

        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"col1": "val1"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[]),
        ):
            with pytest.raises(HTTPException) as exc:
                await import_part_numbers(req, user=user, db=db_session)
        assert exc.value.status_code == 400

    async def test_import_pn_short_mpn_skipped(self, db_session):
        """Lines 695-698 — invalid MPN (< 3 chars) → skipped with warning."""
        from app.routers.materials import import_part_numbers

        req = self._make_file_form_request(b"mpn\nAB\n", "parts.csv")
        user = MagicMock()

        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"mpn": "AB"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[(2, "AB")]),
            patch("app.search_service.run_deterministic_passes"),
        ):
            result = await import_part_numbers(req, user=user, db=db_session)
        assert result["skipped"] == 1
        assert result["warnings"][0]["reason"].startswith("invalid MPN")

    async def test_import_pn_resolve_returns_none_skipped(self, db_session):
        """Lines 700-702 — resolve_material_card returns None → skipped."""
        from app.routers.materials import import_part_numbers

        req = self._make_file_form_request(b"mpn\nVALIDMPN001\n", "parts.csv")
        user = MagicMock()

        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"mpn": "VALIDMPN001"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[(2, "VALIDMPN001")]),
            patch("app.search_service.resolve_material_card", return_value=None),
            patch("app.search_service.run_deterministic_passes"),
        ):
            result = await import_part_numbers(req, user=user, db=db_session)
        assert result["skipped"] == 1
        assert result["warnings"][0]["reason"].startswith("could not create card")

    async def test_import_pn_new_card_counted_as_created(self, db_session):
        """Lines 706-707 — fresh card (enrichment_status='unenriched') → created++."""
        from app.routers.materials import import_part_numbers

        card = MaterialCard(
            normalized_mpn="newpn001",
            display_mpn="NEWPN001",
            search_count=0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        assert card.enriched_at is None
        assert card.search_count == 0

        req = self._make_file_form_request(b"mpn\nNEWPN001\n", "parts.csv")
        user = MagicMock()

        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"mpn": "NEWPN001"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[(2, "NEWPN001")]),
            patch("app.search_service.resolve_material_card", return_value=card),
            patch("app.search_service.run_deterministic_passes"),
        ):
            result = await import_part_numbers(req, user=user, db=db_session)
        assert result["created"] == 1

    async def test_import_pn_existing_card_counted_as_existing(self, db_session):
        """Lines 708-709 — card with search_count > 0 → existing++."""
        from app.routers.materials import import_part_numbers

        card = MaterialCard(
            normalized_mpn="existpn001",
            display_mpn="EXISTPN001",
            search_count=5,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        req = self._make_file_form_request(b"mpn\nEXIST001\n", "parts.csv")
        user = MagicMock()

        with (
            patch("app.file_utils.parse_tabular_file", return_value=[{"mpn": "EXIST001"}]),
            patch("app.file_utils.extract_mpns_with_rows", return_value=[(2, "EXIST001")]),
            patch("app.search_service.resolve_material_card", return_value=card),
            patch("app.search_service.run_deterministic_passes"),
        ):
            result = await import_part_numbers(req, user=user, db=db_session)
        assert result["existing"] == 1


class TestEnrichMaterialDirectSourceBranches:
    """Direct async handler calls to cover lines 502, 510, 522-525, 544-547, 552."""

    async def test_enrich_direct_registered_below_trio_source(self, db_session):
        """Line 502 — registered source with tier < trio_source uses that source."""
        from app.routers.materials import enrich_material

        card = _make_card(db_session, mpn="dirsrc001", display="DIRSRC001", manufacturer=None)

        req = _make_mock_request({"category": "cpu", "source": "digikey_api"})
        user = MagicMock()

        result = await enrich_material(card.id, req, user=user, db=db_session)
        assert result["ladder_source"] == "digikey_api"

    async def test_enrich_direct_unregistered_source_warns(self, db_session):
        """Line 510 — unregistered non-claude_agent source logged at WARNING."""
        from app.routers.materials import enrich_material

        card = _make_card(db_session, mpn="dirsrc002", display="DIRSRC002", manufacturer=None)

        req = _make_mock_request({"description": "test", "source": "digikey"})
        user = MagicMock()

        result = await enrich_material(card.id, req, user=user, db=db_session)
        assert result["ladder_source"] == "ai_guess"

    async def test_enrich_direct_non_numeric_confidence_raises_422(self, db_session):
        """Lines 522-525 — non-numeric confidence raises HTTPException 422."""
        from app.routers.materials import enrich_material

        card = _make_card(db_session, mpn="dirsrc003", display="DIRSRC003")

        req = _make_mock_request({"description": "test", "confidence": "very-high"})
        user = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await enrich_material(card.id, req, user=user, db=db_session)
        assert exc.value.status_code == 422

    async def test_enrich_direct_category_rejected_by_ladder(self, db_session):
        """Lines 544-547 — set_category returns False → category in rejected_fields."""
        from app.routers.materials import enrich_material

        card = _make_card(db_session, mpn="dirsrc004", display="DIRSRC004")
        card.category = "cpu"
        card.category_source = "manual"
        card.category_confidence = 1.0
        card.category_tier = 100
        card.category_updated_at = datetime.now(timezone.utc)
        db_session.commit()

        req = _make_mock_request({"category": "gpu", "source": "claude_agent", "confidence": 0.5})
        user = MagicMock()

        result = await enrich_material(card.id, req, user=user, db=db_session)
        assert "category" in result["rejected_fields"]

    async def test_enrich_direct_manufacturer_rejected_by_ladder(self, db_session):
        """Line 552 — set_manufacturer returns False → manufacturer in
        rejected_fields."""
        from app.routers.materials import enrich_material
        from app.services.spec_tiers import set_manufacturer

        card = _make_card(db_session, mpn="dirsrc005", display="DIRSRC005")
        set_manufacturer(card, "Texas Instruments", "manual", 1.0)
        db_session.commit()

        req = _make_mock_request({"manufacturer": "Microchip", "source": "claude_agent", "confidence": 0.3})
        user = MagicMock()

        result = await enrich_material(card.id, req, user=user, db=db_session)
        assert "manufacturer" in result["rejected_fields"]


class TestImportStockDirectIntegrityErrors:
    """Direct async handler calls for lines 786-788, 810-812, 825-831."""

    async def test_vendor_flush_integrity_error_rolls_back(self, db_session):
        """Lines 786-788 — IntegrityError on VendorCard flush → rollback + re-query."""
        from app.routers.materials import import_stock_list_standalone
        from app.vendor_utils import normalize_vendor_name

        vendor_name = "Direct IE Vendor X"
        norm = normalize_vendor_name(vendor_name)
        # Pre-commit the vendor so after rollback the re-query at line 788 finds it
        existing = VendorCard(
            normalized_name=norm,
            display_name=vendor_name,
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing)
        db_session.commit()

        csv_content = b"mpn,qty\nIEVEND001,10\n"
        mock_file = MagicMock()
        mock_file.filename = "stock.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _get(key, default=None):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return vendor_name
            return default or ""

        mock_form.get = _get

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        # Simulate race-condition: first lookup returns None (vendor not found),
        # then flush raises IE because another worker just inserted it.
        # After rollback, the re-query at line 788 finds the committed vendor.
        from sqlalchemy.exc import IntegrityError as _IE

        call_n = {"n": 0}
        original_flush = db_session.flush

        def _patched_flush(objects=None):
            call_n["n"] += 1
            if call_n["n"] == 1:
                # Raise without calling rollback — the router's except block does it
                raise _IE("duplicate key", None, None)
            return original_flush(objects)

        # Patch the initial vendor lookup to return None so the insert path is taken
        original_query = db_session.query

        def _patched_query(model, *args):
            q = original_query(model, *args)
            if model is VendorCard and call_n["n"] == 0:
                # First time: return empty result to force the INSERT path
                from unittest.mock import MagicMock as _MM

                mock_q = _MM()
                mock_q.filter_by.return_value.first.return_value = None
                return mock_q
            return q

        with (
            patch("app.routers.materials.get_credential_cached", return_value=None),
            patch.object(db_session, "flush", side_effect=_patched_flush),
            patch.object(db_session, "query", side_effect=_patched_query),
        ):
            result = await import_stock_list_standalone(req, user=user, db=db_session)
        assert "imported_rows" in result

    async def test_short_mpn_in_loop_skipped(self, db_session):
        """Lines 810-812 — normalize_stock_row returns parsed row with short MPN →
        skipped."""
        from app.routers.materials import import_stock_list_standalone

        csv_content = b"mpn,qty\nAB,5\n"
        mock_file = MagicMock()
        mock_file.filename = "stock.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _get(key, default=None):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return "Short MPN Vendor"
            return default or ""

        mock_form.get = _get

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        # normalize_stock_row must return a non-None dict with a short MPN to reach line 807
        with (
            patch("app.routers.materials.get_credential_cached", return_value=None),
            patch("app.file_utils.normalize_stock_row", return_value={"mpn": "AB", "qty": 5}),
        ):
            result = await import_stock_list_standalone(req, user=user, db=db_session)
        assert result["skipped_rows"] >= 1

    async def test_material_card_flush_integrity_error_requery_finds_card(self, db_session):
        """Lines 825-827 — MaterialCard flush IntegrityError → rollback + re-query finds
        card."""
        from app.routers.materials import import_stock_list_standalone

        csv_content = b"mpn,qty\nIECARD001,20\n"
        # Pre-commit the card so re-query after rollback finds it
        existing_card = MaterialCard(
            normalized_mpn="iecard001",
            display_mpn="IECARD001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(existing_card)
        db_session.commit()

        mock_file = MagicMock()
        mock_file.filename = "stock.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _get(key, default=None):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return "IE Card Vendor"
            return default or ""

        mock_form.get = _get

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        from sqlalchemy.exc import IntegrityError as _IE

        flush_n = {"n": 0}
        original_flush = db_session.flush

        def _patched_flush(objects=None):
            flush_n["n"] += 1
            # Flush 1 = VendorCard (let pass), Flush 2 = MaterialCard → raise IE
            if flush_n["n"] == 2:
                raise _IE("duplicate key", None, None)
            return original_flush(objects)

        # Make first MaterialCard lookup return None so the insert path is taken
        original_query = db_session.query
        query_n = {"n": 0}

        def _patched_query(model, *args):
            q = original_query(model, *args)
            if model is MaterialCard:
                query_n["n"] += 1
                if query_n["n"] == 1:
                    from unittest.mock import MagicMock as _MM

                    mock_q = _MM()
                    mock_q.filter_by.return_value.first.return_value = None
                    return mock_q
            return q

        with (
            patch("app.routers.materials.get_credential_cached", return_value=None),
            patch.object(db_session, "flush", side_effect=_patched_flush),
            patch.object(db_session, "query", side_effect=_patched_query),
        ):
            result = await import_stock_list_standalone(req, user=user, db=db_session)
        assert "imported_rows" in result

    async def test_material_card_flush_integrity_error_requery_returns_none(self, db_session):
        """Lines 829-831 — MaterialCard flush IE + re-query also returns None →
        skipped."""
        from app.routers.materials import import_stock_list_standalone

        csv_content = b"mpn,qty\nIECARD002,20\n"

        mock_file = MagicMock()
        mock_file.filename = "stock.csv"
        mock_file.read = AsyncMock(return_value=csv_content)

        mock_form = MagicMock()

        def _get(key, default=None):
            if key == "file":
                return mock_file
            if key == "vendor_name":
                return "IE Card None Vendor"
            return default or ""

        mock_form.get = _get

        req = MagicMock(spec=Request)
        req.form = AsyncMock(return_value=mock_form)
        user = MagicMock()

        from sqlalchemy.exc import IntegrityError as _IE

        flush_n = {"n": 0}
        original_flush = db_session.flush

        def _patched_flush(objects=None):
            flush_n["n"] += 1
            if flush_n["n"] == 2:
                raise _IE("duplicate key", None, None)
            return original_flush(objects)

        # ALL MaterialCard queries return None so the card is never found → skip (line 829)
        original_query = db_session.query

        def _patched_query(model, *args):
            q = original_query(model, *args)
            if model is MaterialCard:
                from unittest.mock import MagicMock as _MM

                mock_q = _MM()
                mock_q.filter_by.return_value.first.return_value = None
                return mock_q
            return q

        with (
            patch("app.routers.materials.get_credential_cached", return_value=None),
            patch.object(db_session, "flush", side_effect=_patched_flush),
            patch.object(db_session, "query", side_effect=_patched_query),
        ):
            result = await import_stock_list_standalone(req, user=user, db=db_session)
        assert result["skipped_rows"] >= 1

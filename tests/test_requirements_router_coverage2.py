"""test_requirements_router_coverage2.py — Additional coverage for requirements.py.

Covers: list_requirements 404, add_requirements (batch skip, duplicate detection,
tag propagation), upload_requirements, get_saved_sightings, list_requisition_leads,
add_lead_feedback, import_stock_list, list_requirement_sightings sub-row history.

Auto-search machinery (search_all, ICS/NC enqueue at requirement creation, daily
refresh cron) has been removed — see tests/test_no_auto_search.py.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy.orm import Session

from app.models import (
    MaterialCard,
    Requirement,
    Requisition,
    Sighting,
    SourcingLead,
)

# ── Helpers ───────────────────────────────────────────────────────────────

_VALID_REQ_PAYLOAD = {"primary_mpn": "LM317T", "manufacturer": "ST Micro", "target_qty": 100}


def _make_sighting(db: Session, req_item: Requirement, **kw) -> Sighting:
    defaults = dict(
        requirement_id=req_item.id,
        vendor_name="Arrow",
        vendor_name_normalized="arrow",
        mpn_matched="LM317T",
        source_type="brokerbin",
        qty_available=500,
        unit_price=0.45,
        confidence=80,
        score=50,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    s = Sighting(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_material_card(db: Session, mpn: str = "LM317T") -> MaterialCard:
    from app.utils.normalization import normalize_mpn_key

    card = MaterialCard(
        normalized_mpn=normalize_mpn_key(mpn),
        display_mpn=mpn,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _make_sourcing_lead(db: Session, req: Requisition, req_item: Requirement, **kw) -> SourcingLead:
    import uuid

    defaults = dict(
        lead_id=f"test-{uuid.uuid4().hex[:12]}",
        requisition_id=req.id,
        requirement_id=req_item.id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        match_type="exact",
        vendor_name="Test Vendor",
        vendor_name_normalized="test vendor",
        primary_source_type="manual",
        primary_source_name="test",
        buyer_status="open",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    lead = SourcingLead(**defaults)
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


# ── list_requirements 404 (line 259) ─────────────────────────────────────


class TestListRequirements404:
    def test_list_requirements_not_found(self, client, db_session, test_user):
        resp = client.get("/api/requisitions/999999/requirements")
        assert resp.status_code == 404


# ── add_requirements error paths (lines 386-456) ─────────────────────────


class TestAddRequirements:
    def test_add_requirements_req_not_found(self, client, db_session, test_user):
        resp = client.post(
            "/api/requisitions/999999/requirements",
            json=_VALID_REQ_PAYLOAD,
        )
        assert resp.status_code == 404

    def test_add_requirements_batch_with_invalid_item(self, client, db_session, test_user, test_requisition):
        """Batch add where one item is invalid — skipped item list returned."""
        payload = [
            {"primary_mpn": "LM317T", "manufacturer": "ST Micro", "target_qty": 100},
            {"primary_mpn": "", "manufacturer": "", "target_qty": -1},
        ]
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json=payload,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "created" in data

    def test_add_requirements_single_invalid_raises_422(self, client, db_session, test_user, test_requisition):
        """Single invalid item (missing required manufacturer) raises 422."""
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={"primary_mpn": "LM317T", "target_qty": 100},
            # Missing manufacturer → 422
        )
        assert resp.status_code == 422

    def test_add_requirements_with_material_card(self, client, db_session, test_user, test_requisition):
        """Test add requirement when resolve_material_card returns a card."""
        card = _make_material_card(db_session, "TL431A")
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=card):
            with patch("app.services.tagging.propagate_tags_to_entity"):
                with patch("app.services.task_service.on_requirement_added"):
                    resp = client.post(
                        f"/api/requisitions/{test_requisition.id}/requirements",
                        json={"primary_mpn": "TL431A", "manufacturer": "TI", "target_qty": 50},
                    )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 1

    def test_add_requirements_task_autoassign_exception(self, client, db_session, test_user, test_requisition):
        """task_service.on_requirement_added can raise — must be caught silently."""
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            with patch(
                "app.services.task_service.on_requirement_added",
                side_effect=Exception("task error"),
            ):
                resp = client.post(
                    f"/api/requisitions/{test_requisition.id}/requirements",
                    json=[{"primary_mpn": "NE555", "manufacturer": "TI", "target_qty": 200}],
                )
        assert resp.status_code == 200

    def test_add_requirements_with_skipped_returns_skipped_key(self, client, db_session, test_user, test_requisition):
        """Valid batch payload — created and duplicates in response."""
        payload = [
            {"primary_mpn": "NE555", "manufacturer": "TI", "target_qty": 100},
        ]
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json=payload,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "created" in data
        assert "duplicates" in data

    def test_add_requirements_with_customer_site_duplicate_detection(self, client, db_session, test_user):
        """When customer_site_id set and material_card_id set, duplicate detection
        runs."""
        from app.models import Company, CustomerSite

        company = Company(
            name="DupTest Co",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(company)
        db_session.flush()

        site = CustomerSite(
            company_id=company.id,
            site_name="Main Site",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(site)
        db_session.flush()

        req = Requisition(
            name="DupReq",
            customer_name="DupTest Co",
            customer_site_id=site.id,
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        db_session.refresh(req)

        card = _make_material_card(db_session, "BC547")

        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=card):
            with patch("app.services.tagging.propagate_tags_to_entity"):
                with patch("app.services.task_service.on_requirement_added"):
                    resp = client.post(
                        f"/api/requisitions/{req.id}/requirements",
                        json={"primary_mpn": "BC547", "manufacturer": "ST", "target_qty": 100},
                    )
        assert resp.status_code == 200
        data = resp.json()
        assert "duplicates" in data


# ── get_saved_sightings 404 (line 914) ────────────────────────────────────


class TestGetSavedSightings:
    def test_get_saved_sightings_req_not_found(self, client, db_session, test_user):
        resp = client.get("/api/requisitions/999999/sightings")
        assert resp.status_code == 404

    def test_get_saved_sightings_with_history(self, client, db_session, test_user, test_requisition):
        """Saved sightings with a material card triggers _history_to_result (line
        1025)."""
        card = _make_material_card(db_session, "LM317T")
        req_item = test_requisition.requirements[0]
        req_item.material_card_id = card.id
        db_session.commit()

        _make_sighting(db_session, req_item, mpn_matched="LM317T")

        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200


# ── list_requisition_leads 404 (line 1053) ───────────────────────────────


class TestListRequisitionLeads:
    def test_leads_req_not_found(self, client, db_session, test_user):
        resp = client.get("/api/requisitions/999999/leads")
        assert resp.status_code == 404

    def test_leads_with_status_filter(self, client, db_session, test_user, test_requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/leads?statuses=open,pending")
        assert resp.status_code == 200


# ── add_lead_feedback 404 (line 1167) ────────────────────────────────────


class TestAddLeadFeedback:
    def test_lead_feedback_lead_not_found(self, client, db_session, test_user):
        resp = client.post(
            "/api/leads/999999/feedback",
            json={"note": "test note"},
        )
        assert resp.status_code == 404

    def test_lead_feedback_append_returns_none(self, client, db_session, test_user, test_requisition):
        """append_lead_feedback returning None → 404."""
        req_item = test_requisition.requirements[0]
        lead = _make_sourcing_lead(db_session, test_requisition, req_item)

        with patch(
            "app.routers.requisitions.requirements.append_lead_feedback",
            return_value=None,
        ):
            resp = client.post(
                f"/api/leads/{lead.id}/feedback",
                json={"note": "test note"},
            )
        assert resp.status_code == 404


# ── import_stock_list error paths (lines 1201, 1207, 1212-1281) ──────────


class TestImportStockList:
    def test_import_stock_req_not_found(self, client, db_session, test_user):
        resp = client.post(
            "/api/requisitions/999999/import-stock",
            data={"vendor_name": "TestVendor"},
            files={"file": ("test.csv", b"mpn,qty\nLM317T,100", "text/csv")},
        )
        assert resp.status_code == 404

    def test_import_stock_no_file(self, client, db_session, test_user, test_requisition):
        """No file uploaded → 400."""
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/import-stock",
            data={"vendor_name": "TestVendor"},
        )
        assert resp.status_code == 400

    def test_import_stock_file_too_large(self, client, db_session, test_user, test_requisition):
        """File > 10MB → 413."""
        big_content = b"x" * (10_000_001)
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/import-stock",
            data={"vendor_name": "TestVendor"},
            files={"file": ("big.csv", big_content, "text/csv")},
        )
        assert resp.status_code == 413

    def test_import_stock_csv_with_matching_mpn(self, client, db_session, test_user, test_requisition):
        """CSV that matches a requirement MPN — creates a sighting."""
        csv_content = b"mpn,qty,price\nLM317T,500,0.45\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "TestVendor"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "imported_rows" in data
        assert "matched_sightings" in data

    def test_import_stock_csv_no_matching_mpn(self, client, db_session, test_user, test_requisition):
        """CSV with MPN that does not match any requirement."""
        csv_content = b"mpn,qty,price\nXYZ999,100,0.10\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "TestVendor"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_sightings"] == 0

    def test_import_stock_exception_rolls_back(self, client, db_session, test_user, test_requisition):
        """Exception during import → 500."""
        csv_content = b"mpn,qty\nLM317T,100\n"
        with patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            side_effect=Exception("db error"),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "TestVendor"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 500

    def test_import_stock_with_material_card(self, client, db_session, test_user, test_requisition):
        """CSV where resolve_material_card returns a card — material_card_id is set."""
        card = _make_material_card(db_session, "LM317T")
        csv_content = b"mpn,qty,price\nLM317T,500,0.45\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=card):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "TestVendor"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_sightings"] >= 1


# ── list_requirement_sightings with substitute card history (line 1342) ──


class TestListRequirementSightings:
    def test_sightings_with_sub_mpn_material_card(self, client, db_session, test_user, test_requisition):
        """Requirement with substitute MPNs that have material cards triggers sub_rows
        path."""
        req_item = test_requisition.requirements[0]

        # Add a substitute to the requirement (string format)
        req_item.substitutes = ["TL431A"]
        db_session.commit()

        # Create material card for the substitute
        _make_material_card(db_session, "TL431A")

        _make_sighting(db_session, req_item, mpn_matched="LM317T")

        resp = client.get(f"/api/requirements/{req_item.id}/sightings")
        assert resp.status_code == 200

    def test_sightings_with_dict_substitute(self, client, db_session, test_user, test_requisition):
        """Requirement with dict-format substitutes (mpn key)."""
        req_item = test_requisition.requirements[0]

        req_item.substitutes = [{"mpn": "TL431A", "manufacturer": "TI"}]
        db_session.commit()

        _make_material_card(db_session, "TL431A")

        resp = client.get(f"/api/requirements/{req_item.id}/sightings")
        assert resp.status_code == 200


# ── upload_requirements parse edge case (line 603) ───────────────────────


class TestUploadRequirements:
    def test_upload_csv_with_substitute_columns(self, client, db_session, test_user, test_requisition):
        """CSV with sub_1/sub_2 columns normalizes MPNs (exercises dedup loop)."""
        csv_content = b"primary_mpn,target_qty,sub_1,sub_2\nLM317T,100,TL431A,\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/upload",
                files={"file": ("parts.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200

    def test_upload_req_not_found(self, client, db_session, test_user):
        csv_content = b"primary_mpn,target_qty\nLM317T,100\n"
        resp = client.post(
            "/api/requisitions/999999/upload",
            files={"file": ("parts.csv", csv_content, "text/csv")},
        )
        assert resp.status_code == 404

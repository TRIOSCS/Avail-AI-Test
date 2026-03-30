"""test_requirements_router_coverage.py — Additional coverage for requirements.py.

Covers uncovered branches including: upload_requirements, import-stock,
search_all with requirement_ids, search_one, _dedupe_substitutes, get_saved_sightings
with data, list_requirement_sightings with substitutes, list_requirement_offers
with historical data, leads with data, lead status/feedback, add_requirements
with material_card, delete_requirement auth, update_requirement with resolve failure.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    ChangeLog,
    MaterialCard,
    Offer,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_requirement(db: Session, req: Requisition, **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn="LM317T",
        normalized_mpn="lm317t",
        target_qty=100,
        target_price=0.50,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = Requirement(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


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


def _make_offer(db: Session, req: Requisition, req_item: Requirement, user: User, **kw) -> Offer:
    defaults = dict(
        requisition_id=req.id,
        requirement_id=req_item.id,
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        mpn="LM317T",
        normalized_mpn="lm317t",
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    o = Offer(**defaults)
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def _make_material_card(db: Session, mpn: str = "LM317T") -> MaterialCard:
    from app.utils.normalization import normalize_mpn_key

    mc = MaterialCard(
        display_mpn=mpn,
        normalized_mpn=normalize_mpn_key(mpn),
        manufacturer="Texas Instruments",
        created_at=datetime.now(timezone.utc),
    )
    db.add(mc)
    db.commit()
    db.refresh(mc)
    return mc


# ══════════════════════════════════════════════════════════════════════════
# _dedupe_substitutes helper function
# ══════════════════════════════════════════════════════════════════════════


class TestDedupeSubstitutes:
    def test_basic_dedup(self):
        from app.routers.requisitions.requirements import _dedupe_substitutes

        result = _dedupe_substitutes(["NE555P", "LM317T", "NE555P"], "LM317T")
        mpns = [r["mpn"] for r in result]
        assert "LM317T" not in mpns  # primary excluded
        assert len([m for m in mpns if "NE555P" in m.upper()]) == 1  # deduped

    def test_empty_list(self):
        from app.routers.requisitions.requirements import _dedupe_substitutes

        assert _dedupe_substitutes([], "LM317T") == []

    def test_normalize_short_mpn(self):
        from app.routers.requisitions.requirements import _dedupe_substitutes

        # Very short strings that normalize_mpn returns None for
        result = _dedupe_substitutes(["AB"], "LM317T")
        # Should handle gracefully (AB < 3 chars may be excluded by normalize_mpn)
        assert isinstance(result, list)


# ══════════════════════════════════════════════════════════════════════════
# add_requirements with material_card_id
# ══════════════════════════════════════════════════════════════════════════


class TestAddRequirementsWithMaterialCard:
    def test_add_with_material_card(self, client, db_session, test_user, test_requisition):
        mc = _make_material_card(db_session, "NE555P")
        with patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=mc,
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json={"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": 100},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 1

    def test_add_with_resolve_failure(self, client, db_session, test_user, test_requisition):
        with patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            side_effect=Exception("DB error"),
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json={"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": 100},
            )
        assert resp.status_code == 200
        # Should still create the requirement, just without material_card_id

    def test_add_with_customer_site_and_material_card(self, client, db_session, test_user, test_requisition):
        from app.models import Company, CustomerSite

        co = Company(name="Site Corp", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()
        test_requisition.customer_site_id = site.id
        db_session.commit()
        mc = _make_material_card(db_session, "NE555P")
        with patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            return_value=mc,
        ):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json={"primary_mpn": "NE555P", "manufacturer": "TI"},
            )
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# upload_requirements via CSV
# ══════════════════════════════════════════════════════════════════════════


class TestUploadRequirements:
    def test_upload_csv(self, client, db_session, test_user, test_requisition):
        csv_content = b"mpn,qty,condition\nLM317T,100,new\nNE555P,50,used\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None), \
             patch("app.services.tagging.propagate_tags_to_entity", return_value=None), \
             patch("app.routers.requisitions.requirements.enqueue_for_nc_search"), \
             patch("app.routers.requisitions.requirements.enqueue_for_ics_search"), \
             patch("app.routers.requisitions.requirements.SessionLocal") as mock_sl:
            mock_sl.return_value.__enter__ = lambda s, *a: db_session
            mock_sl.return_value.__exit__ = lambda s, *a: None
            mock_sl.return_value = db_session
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/upload",
                files={"file": ("parts.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "created" in data

    def test_upload_not_found(self, client):
        csv_content = b"mpn,qty\nLM317T,100\n"
        resp = client.post(
            "/api/requisitions/99999/upload",
            files={"file": ("parts.csv", csv_content, "text/csv")},
        )
        assert resp.status_code == 404

    def test_upload_file_too_large(self, client, db_session, test_user, test_requisition):
        big_content = b"x" * (10_000_001)
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/upload",
            files={"file": ("big.csv", big_content, "text/csv")},
        )
        assert resp.status_code == 413

    def test_upload_no_mpn_rows_skipped(self, client, db_session, test_user, test_requisition):
        csv_content = b"notmpn,qty\nfoo,100\nbar,50\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/upload",
                files={"file": ("noparts.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        assert resp.json()["created"] == 0

    def test_upload_with_substitutes_columns(self, client, db_session, test_user, test_requisition):
        csv_content = b"mpn,qty,sub_1,sub_2\nLM317T,100,NE555P,LM7805\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None), \
             patch("app.services.tagging.propagate_tags_to_entity", return_value=None), \
             patch("app.routers.requisitions.requirements.enqueue_for_nc_search"), \
             patch("app.routers.requisitions.requirements.enqueue_for_ics_search"), \
             patch("app.routers.requisitions.requirements.SessionLocal", return_value=db_session):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/upload",
                files={"file": ("subs.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# delete_requirement authorization
# ══════════════════════════════════════════════════════════════════════════


class TestDeleteRequirementAuth:
    def test_delete_wrong_req(self, client, db_session, test_user, test_requisition):
        """Delete should fail with 403 when req is not accessible."""
        from app.constants import RequisitionStatus

        # Create a second requisition owned by a different user — but since auth
        # is overridden in tests to always return test_user, we simulate the
        # access denial via get_req_for_user returning None by using req from
        # a completely different user's context. Instead, test the 404 path via
        # a requirement that belongs to a req not accessible.
        req_item = test_requisition.requirements[0]
        # Patch get_req_for_user to return None to simulate auth failure
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.delete(f"/api/requirements/{req_item.id}")
            assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# update_requirement with resolve_material_card failure
# ══════════════════════════════════════════════════════════════════════════


class TestUpdateRequirementEdgeCases:
    def test_update_with_resolve_failure(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        with patch(
            "app.routers.requisitions.requirements.resolve_material_card",
            side_effect=Exception("DB fail"),
        ):
            resp = client.put(
                f"/api/requirements/{req_item.id}",
                json={"primary_mpn": "NE555P"},
            )
        assert resp.status_code == 200
        # Should still succeed even when resolve fails

    def test_update_need_by_date(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.put(
            f"/api/requirements/{req_item.id}",
            json={"need_by_date": "2026-07-01"},
        )
        assert resp.status_code == 200

    def test_update_not_authorized(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.put(f"/api/requirements/{req_item.id}", json={"target_qty": 500})
            assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# search_all with requirement_ids filter
# ══════════════════════════════════════════════════════════════════════════


class TestSearchAllEdgeCases:
    @patch("app.routers.requisitions.requirements.enqueue_for_nc_search")
    @patch("app.routers.requisitions.requirements.enqueue_for_ics_search")
    def test_search_all_with_requirement_ids(self, mock_ics, mock_nc, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        with patch("app.routers.requisitions._enrich_with_vendor_cards"):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/search",
                json={"requirement_ids": [req_item.id]},
            )
        assert resp.status_code == 200

    @patch("app.routers.requisitions.requirements.enqueue_for_nc_search")
    @patch("app.routers.requisitions.requirements.enqueue_for_ics_search")
    def test_search_all_not_found(self, mock_ics, mock_nc, client):
        resp = client.post("/api/requisitions/99999/search")
        assert resp.status_code == 404

    @patch("app.routers.requisitions.requirements.enqueue_for_nc_search")
    @patch("app.routers.requisitions.requirements.enqueue_for_ics_search")
    def test_search_all_draft_status(self, mock_ics, mock_nc, client, db_session, test_user):
        from app.constants import RequisitionStatus

        req = Requisition(
            name="DRAFT-REQ",
            status=RequisitionStatus.DRAFT,
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()
        with patch("app.routers.requisitions._enrich_with_vendor_cards"):
            with patch("app.services.requisition_state.transition"):
                resp = client.post(f"/api/requisitions/{req.id}/search")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# search_one (single requirement search)
# ══════════════════════════════════════════════════════════════════════════


class TestSearchOne:
    @patch("app.routers.requisitions.requirements.enqueue_for_nc_search")
    @patch("app.routers.requisitions.requirements.enqueue_for_ics_search")
    def test_search_one_basic(self, mock_ics, mock_nc, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        with patch("app.routers.requisitions._enrich_with_vendor_cards"):
            resp = client.post(f"/api/requirements/{req_item.id}/search")
        assert resp.status_code == 200
        data = resp.json()
        assert "sightings" in data
        assert "source_stats" in data

    def test_search_one_not_found(self, client):
        resp = client.post("/api/requirements/99999/search")
        assert resp.status_code == 404

    @patch("app.routers.requisitions.requirements.enqueue_for_nc_search")
    @patch("app.routers.requisitions.requirements.enqueue_for_ics_search")
    def test_search_one_not_authorized(self, mock_ics, mock_nc, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.post(f"/api/requirements/{req_item.id}/search")
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# get_saved_sightings with data
# ══════════════════════════════════════════════════════════════════════════


class TestGetSavedSightingsWithData:
    @patch("app.routers.requisitions._enrich_with_vendor_cards")
    def test_with_sightings(self, mock_enrich, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        _make_sighting(db_session, req_item)
        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200
        data = resp.json()
        assert str(req_item.id) in data

    @patch("app.routers.requisitions._enrich_with_vendor_cards")
    def test_with_material_card(self, mock_enrich, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        mc = _make_material_card(db_session, "LM317T")
        req_item.material_card_id = mc.id
        db_session.commit()
        _make_sighting(db_session, req_item)
        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200

    def test_sightings_empty_requirements(self, client, db_session, test_user):
        req = Requisition(
            name="EMPTY-REQ",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/sightings")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {}


# ══════════════════════════════════════════════════════════════════════════
# list_requirement_sightings with substitutes
# ══════════════════════════════════════════════════════════════════════════


class TestListRequirementSightingsExtra:
    @patch("app.routers.requisitions._enrich_with_vendor_cards")
    def test_with_empty_sightings(self, mock_enrich, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.get(f"/api/requirements/{req_item.id}/sightings")
        assert resp.status_code == 200
        data = resp.json()
        assert "sightings" in data
        assert data["sightings"] == []

    @patch("app.routers.requisitions._enrich_with_vendor_cards")
    def test_with_substitutes(self, mock_enrich, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.substitutes = [{"mpn": "NE555P", "manufacturer": "TI"}]
        db_session.commit()
        resp = client.get(f"/api/requirements/{req_item.id}/sightings")
        assert resp.status_code == 200

    def test_not_authorized(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.get(f"/api/requirements/{req_item.id}/sightings")
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# list_requirement_offers — historical data path
# ══════════════════════════════════════════════════════════════════════════


class TestListRequirementOffersHistorical:
    def test_with_material_card_and_historical_offer(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        mc = _make_material_card(db_session, "LM317T")
        req_item.material_card_id = mc.id
        db_session.commit()
        # Create an offer on a DIFFERENT requisition with same material_card_id
        other_req = Requisition(
            name="OTHER-REQ",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_req)
        db_session.flush()
        other_item = Requirement(
            requisition_id=other_req.id,
            primary_mpn="LM317T",
            target_qty=50,
            material_card_id=mc.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_item)
        db_session.flush()
        hist_offer = Offer(
            requisition_id=other_req.id,
            requirement_id=other_item.id,
            vendor_name="DigiKey",
            mpn="LM317T",
            qty_available=200,
            unit_price=0.60,
            material_card_id=mc.id,
            entered_by_id=test_user.id,
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(hist_offer)
        db_session.commit()
        resp = client.get(f"/api/requirements/{req_item.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        # Should include historical offer
        historical = [o for o in data if o["is_historical"]]
        assert len(historical) >= 1

    def test_with_substitute_material_card(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        req_item.substitutes = [{"mpn": "NE555P", "manufacturer": "TI"}]
        db_session.commit()
        # Create a material card for the substitute
        mc = _make_material_card(db_session, "NE555P")
        resp = client.get(f"/api/requirements/{req_item.id}/offers")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# import-stock endpoint
# ══════════════════════════════════════════════════════════════════════════


class TestImportStock:
    def test_import_stock_no_file(self, client, db_session, test_user, test_requisition):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/import-stock",
            data={"vendor_name": "Arrow"},
        )
        assert resp.status_code == 400

    def test_import_stock_not_found(self, client):
        csv_content = b"mpn,qty,price\nLM317T,100,0.50\n"
        resp = client.post(
            "/api/requisitions/99999/import-stock",
            data={"vendor_name": "Arrow", "file": ("stock.csv", csv_content, "text/csv")},
        )
        assert resp.status_code == 404

    def test_import_stock_csv(self, client, db_session, test_user, test_requisition):
        csv_content = b"mpn,qty,price,condition\nLM317T,100,0.50,new\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "Arrow"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "imported_rows" in data

    def test_import_stock_file_too_large(self, client, db_session, test_user, test_requisition):
        big_content = b"x" * (10_000_001)
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/import-stock",
            data={"vendor_name": "Arrow"},
            files={"file": ("big.csv", big_content, "text/csv")},
        )
        assert resp.status_code == 413

    def test_import_stock_unmatched_mpn(self, client, db_session, test_user, test_requisition):
        csv_content = b"mpn,qty,price\nXYZ999,100,0.50\n"
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "Arrow"},
                files={"file": ("stock.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_sightings"] == 0


# ══════════════════════════════════════════════════════════════════════════
# Leads endpoints with data
# ══════════════════════════════════════════════════════════════════════════


class TestLeadsWithData:
    def test_list_leads_with_status_filter(self, client, db_session, test_user, test_requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/leads?statuses=open,contacted")
        assert resp.status_code == 200

    def test_leads_queue_all_status(self, client, db_session, test_user):
        resp = client.get("/api/leads/queue?status=all")
        assert resp.status_code == 200

    def test_leads_queue_with_status(self, client, db_session, test_user):
        resp = client.get("/api/leads/queue?status=contacted")
        assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════════
# Lead with actual data (create SourcingLead)
# ══════════════════════════════════════════════════════════════════════════


class TestSourcingLeadCRUD:
    def _make_sourcing_lead(self, db, req, user):
        from app.models.sourcing_lead import SourcingLead

        lead = SourcingLead(
            requisition_id=req.id,
            requirement_id=req.requirements[0].id,
            lead_id="LEAD-TEST-001",
            vendor_name="Arrow Electronics",
            vendor_name_normalized="arrow electronics",
            part_number_requested="LM317T",
            part_number_matched="LM317T",
            match_type="exact",
            primary_source_type="brokerbin",
            primary_source_name="BrokerBin",
            confidence_score=80.0,
            confidence_band="high",
            vendor_safety_score=75.0,
            vendor_safety_band="medium",
            buyer_status="open",
            buyer_owner_user_id=user.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
        return lead

    def test_list_leads_with_lead(self, client, db_session, test_user, test_requisition):
        self._make_sourcing_lead(db_session, test_requisition, test_user)
        resp = client.get(f"/api/requisitions/{test_requisition.id}/leads")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    def test_get_lead_detail(self, client, db_session, test_user, test_requisition):
        lead = self._make_sourcing_lead(db_session, test_requisition, test_user)
        resp = client.get(f"/api/leads/{lead.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_name"] == "Arrow Electronics"

    def test_get_lead_not_authorized(self, client, db_session, test_user, test_requisition):
        lead = self._make_sourcing_lead(db_session, test_requisition, test_user)
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.get(f"/api/leads/{lead.id}")
        assert resp.status_code == 403

    def test_patch_lead_status_not_authorized(self, client, db_session, test_user, test_requisition):
        lead = self._make_sourcing_lead(db_session, test_requisition, test_user)
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.patch(f"/api/leads/{lead.id}/status", json={"status": "contacted"})
        assert resp.status_code == 403

    def test_add_lead_feedback_not_authorized(self, client, db_session, test_user, test_requisition):
        lead = self._make_sourcing_lead(db_session, test_requisition, test_user)
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.post(f"/api/leads/{lead.id}/feedback", json={"note": "Test note"})
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# Mark unavailable — not authorized path
# ══════════════════════════════════════════════════════════════════════════


class TestMarkUnavailableAuth:
    def test_mark_unavailable_no_req_access(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        s = _make_sighting(db_session, req_item)
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.put(f"/api/sightings/{s.id}/unavailable", json={"unavailable": True})
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# Task with assigned_to_id + due_at
# ══════════════════════════════════════════════════════════════════════════


class TestRequirementTaskWithAssignee:
    def test_create_task_with_assignee(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        resp = client.post(
            f"/api/requirements/{req_item.id}/tasks",
            json={
                "title": "Task with assignee",
                "assigned_to_id": test_user.id,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Task with assignee"


# ══════════════════════════════════════════════════════════════════════════
# list_requirement_history — offer changes
# ══════════════════════════════════════════════════════════════════════════


class TestRequirementHistoryOfferChanges:
    def test_history_with_offer_changes(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        offer = _make_offer(db_session, test_requisition, req_item, test_user)
        cl = ChangeLog(
            entity_type="offer",
            entity_id=offer.id,
            user_id=test_user.id,
            field_name="unit_price",
            old_value="0.50",
            new_value="0.45",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(cl)
        db_session.commit()
        resp = client.get(f"/api/requirements/{req_item.id}/history")
        assert resp.status_code == 200
        data = resp.json()
        offer_changes = [e for e in data if e.get("entity") == "offer"]
        assert len(offer_changes) >= 1


# ══════════════════════════════════════════════════════════════════════════
# Batch add requirements (array input)
# ══════════════════════════════════════════════════════════════════════════


class TestBatchAddRequirements:
    def test_batch_add_valid(self, client, db_session, test_user, test_requisition):
        """Batch POST with a list of requirements."""
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None), \
             patch("app.routers.requisitions.requirements.SessionLocal", return_value=db_session):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json=[
                    {"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": 100},
                    {"primary_mpn": "LM7805", "manufacturer": "TI", "target_qty": 50},
                ],
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["created"]) == 2

    def test_batch_add_partial_invalid(self, client, db_session, test_user, test_requisition):
        """Batch POST where one item is invalid — should be skipped."""
        with patch("app.routers.requisitions.requirements.resolve_material_card", return_value=None), \
             patch("app.routers.requisitions.requirements.SessionLocal", return_value=db_session):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json=[
                    {"primary_mpn": "NE555P", "target_qty": 100},
                    {"no_mpn": "invalid_item"},  # missing primary_mpn
                ],
            )
        # Even if some are invalid in batch mode, response should be 200 with skipped
        assert resp.status_code in (200, 422)

    def test_single_add_invalid_validation(self, client, db_session, test_user, test_requisition):
        """Single POST with invalid payload returns 422."""
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/requirements",
            json={"target_qty": 100},  # missing primary_mpn
        )
        assert resp.status_code == 422


# ══════════════════════════════════════════════════════════════════════════
# Lead status patch — success path
# ══════════════════════════════════════════════════════════════════════════


class TestPatchLeadStatus:
    def _make_sourcing_lead(self, db, req, user):
        from app.models.sourcing_lead import SourcingLead

        lead = SourcingLead(
            requisition_id=req.id,
            requirement_id=req.requirements[0].id,
            lead_id="LEAD-STATUS-001",
            vendor_name="Digi-Key",
            vendor_name_normalized="digi-key",
            part_number_requested="LM317T",
            part_number_matched="LM317T",
            match_type="exact",
            primary_source_type="digikey",
            primary_source_name="DigiKey",
            confidence_score=85.0,
            confidence_band="high",
            vendor_safety_score=80.0,
            vendor_safety_band="medium",
            buyer_status="new",
            buyer_owner_user_id=user.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(lead)
        db.commit()
        db.refresh(lead)
        return lead

    def test_patch_status_success(self, client, db_session, test_user, test_requisition):
        lead = self._make_sourcing_lead(db_session, test_requisition, test_user)
        with patch(
            "app.routers.requisitions.requirements.update_lead_status",
            return_value=lead,
        ):
            resp = client.patch(
                f"/api/leads/{lead.id}/status",
                json={"status": "contacted"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_patch_status_not_found(self, client, db_session, test_user):
        resp = client.patch("/api/leads/99999/status", json={"status": "contacted"})
        assert resp.status_code == 404

    def test_add_feedback_success(self, client, db_session, test_user, test_requisition):
        lead = self._make_sourcing_lead(db_session, test_requisition, test_user)
        with patch(
            "app.routers.requisitions.requirements.append_lead_feedback",
            return_value=lead,
        ):
            resp = client.post(
                f"/api/leads/{lead.id}/feedback",
                json={"note": "Called and got a quote"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_add_feedback_not_found(self, client, db_session, test_user):
        resp = client.post("/api/leads/99999/feedback", json={"note": "test"})
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Upload requirements — parse error path
# ══════════════════════════════════════════════════════════════════════════


class TestUploadParseError:
    def test_upload_parse_error(self, client, db_session, test_user, test_requisition):
        """Simulates a parse error in parse_tabular_file."""
        with patch(
            "app.routers.requisitions.requirements.resolve_material_card", return_value=None
        ), patch("app.file_utils.parse_tabular_file", side_effect=ValueError("bad csv")):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/upload",
                files={"file": ("bad.csv", b"garbage", "text/csv")},
            )
        assert resp.status_code in (400, 500)


# ══════════════════════════════════════════════════════════════════════════
# search_all — exception in search result (error handling branch)
# ══════════════════════════════════════════════════════════════════════════


class TestToggleQuoteSelection:
    def test_toggle_quote_selection_success(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        offer = _make_offer(db_session, test_requisition, req_item, test_user)
        resp = client.post(f"/api/offers/{offer.id}/toggle-quote-selection")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_toggle_quote_selection_not_found(self, client):
        resp = client.post("/api/offers/99999/toggle-quote-selection")
        assert resp.status_code == 404

    def test_toggle_quote_selection_not_authorized(self, client, db_session, test_user, test_requisition):
        req_item = test_requisition.requirements[0]
        offer = _make_offer(db_session, test_requisition, req_item, test_user)
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.post(f"/api/offers/{offer.id}/toggle-quote-selection")
        assert resp.status_code == 403


class TestAddRequirementsDuplicateDetection:
    def test_add_with_customer_site_detects_dup(self, client, db_session, test_user, test_requisition):
        """Cover duplicate detection block (lines 501-529) by adding with customer_site_id."""
        from app.models import Company, CustomerSite

        co = Company(name="DupCo", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()
        test_requisition.customer_site_id = site.id
        db_session.commit()

        mc = _make_material_card(db_session, "NE555P")
        with patch(
            "app.routers.requisitions.requirements.resolve_material_card", return_value=mc
        ), patch("app.routers.requisitions.requirements.SessionLocal", return_value=db_session):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json={"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": 100},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "duplicates" in data


class TestSearchAllErrorBranch:
    @patch("app.routers.requisitions.requirements.enqueue_for_nc_search")
    @patch("app.routers.requisitions.requirements.enqueue_for_ics_search")
    @patch("app.routers.requisitions._enrich_with_vendor_cards")
    def test_search_all_with_exception_in_result(
        self, mock_enrich, mock_nc, mock_ics, client, db_session, test_user, test_requisition
    ):
        """Tests the branch where search_requirement returns an Exception."""

        async def _failing_search(req, db):
            raise RuntimeError("connector failed")

        with patch(
            "app.routers.requisitions.search_requirement",
            new=_failing_search,
        ):
            resp = client.post(f"/api/requisitions/{test_requisition.id}/search")
        # Should return 200 even when individual searches fail
        assert resp.status_code == 200

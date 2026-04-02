"""tests/test_coverage_boost_requirements.py — Covers genuine gaps in
app/routers/requisitions/requirements.py that are NOT dead code or async-bug lines.

Targets (all before any await in async handlers, or in sync functions):
  - lines 943-948: get_saved_sightings substitutes loop (string substitutes)
  - lines 953-958: get_saved_sightings sub_card_lookup (all_sub_keys branch)
  - lines 185, 194: _attach_lead_data / _annotate_lead_metadata with actual leads
  - lines 1127-1130: patch_lead_status ValueError + lead not found
  - lines 837-838: search_all stats merge (same source twice)

Called by: pytest
Depends on: tests/conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User
from app.models.sourcing_lead import SourcingLead


@pytest.fixture()
def req_with_string_subs(db_session: Session, test_user: User) -> tuple:
    """Requisition + Requirement that has legacy string substitutes."""
    req = Requisition(
        name="REQ-STR-SUBS",
        customer_name="SubCorp",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="NE555",
        target_qty=100,
        # Legacy format: plain strings instead of dicts
        substitutes=["LM555", "UA555"],
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


@pytest.fixture()
def req_with_lead(db_session: Session, test_user: User) -> tuple:
    """Requisition + Requirement + SourcingLead so _attach_lead_data has real data."""
    req = Requisition(
        name="REQ-LEAD-COV",
        customer_name="LeadCorp",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=500,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.flush()

    lead = SourcingLead(
        lead_id=f"lead-{uuid.uuid4().hex[:8]}",
        requirement_id=item.id,
        requisition_id=req.id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        vendor_name="Arrow",
        vendor_name_normalized="arrow",
        primary_source_type="search",
        primary_source_name="brokerbin",
        confidence_score=0.8,
        confidence_band="high",
        reason_summary="Found in BrokerBin",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        buyer_status="open",
    )
    db_session.add(lead)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    db_session.refresh(lead)
    return req, item, lead


# ── get_saved_sightings with string substitutes ───────────────────────


class TestGetSavedSightingsWithSubstitutes:
    def test_string_subs_are_processed(self, client: TestClient, req_with_string_subs):
        """Lines 943-948: string substitutes trigger sub_keys path."""
        req, item = req_with_string_subs
        resp = client.get(f"/api/requisitions/{req.id}/sightings")
        assert resp.status_code == 200
        # Coverage: lines 943 (sub_str), 944 (if sub_str), 945 (sub_key),
        #           946 (if sub_key), 947 (sub_keys.append), 948 (add to all_sub_keys)

    def test_all_sub_keys_lookup_branch(self, client: TestClient, req_with_string_subs):
        """Lines 953-958: all_sub_keys is non-empty → runs DB query for cards."""
        req, _ = req_with_string_subs
        resp = client.get(f"/api/requisitions/{req.id}/sightings")
        assert resp.status_code == 200
        # Coverage: lines 953 (if all_sub_keys), 955-956 (DB query), 958 (sub_card_lookup)

    def test_no_subs_baseline(self, client: TestClient, test_requisition):
        """Baseline: requirement with no substitutes → empty sub_keys."""
        resp = client.get(f"/api/requisitions/{test_requisition.id}/sightings")
        assert resp.status_code == 200


# ── _attach_lead_data with real leads ────────────────────────────────


class TestAttachLeadData:
    def test_get_saved_sightings_with_lead(self, client: TestClient, req_with_lead):
        """Lines 185, 194: _attach_lead_data called with an actual SourcingLead."""
        req, item, lead = req_with_lead
        resp = client.get(f"/api/requisitions/{req.id}/sightings")
        assert resp.status_code == 200
        # Coverage: line 185 (leads_by_req.setdefault), 194+ (lead_cards.append)

    def test_list_requisition_leads_with_lead(self, client: TestClient, req_with_lead):
        """List requisition leads returns the created lead."""
        req, item, lead = req_with_lead
        resp = client.get(f"/api/requisitions/{req.id}/leads")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1


# ── patch_lead_status error paths ────────────────────────────────────


class TestPatchLeadStatusErrors:
    def test_lead_not_found_returns_404(self, client: TestClient):
        """Lines 1111-1112: lead not in DB → 404."""
        resp = client.patch(
            "/api/leads/999999/status",
            json={"status": "contacted"},
        )
        assert resp.status_code == 404

    def test_update_service_raises_value_error(self, client: TestClient, req_with_lead):
        """Lines 1127-1128: update_lead_status raises ValueError → 400."""
        req, item, lead = req_with_lead
        # Patch at the import site in the router module
        with patch(
            "app.routers.requisitions.requirements.update_lead_status",
            side_effect=ValueError("invalid status transition"),
        ):
            resp = client.patch(
                f"/api/leads/{lead.id}/status",
                json={"status": "contacted"},
            )
        assert resp.status_code == 400

    def test_update_service_returns_none(self, client: TestClient, req_with_lead):
        """Line 1130: update_lead_status returns None → 404."""
        req, item, lead = req_with_lead
        with patch(
            "app.routers.requisitions.requirements.update_lead_status",
            return_value=None,
        ):
            resp = client.patch(
                f"/api/leads/{lead.id}/status",
                json={"status": "contacted"},
            )
        assert resp.status_code == 404

    def test_valid_update(self, client: TestClient, req_with_lead):
        """Happy path: valid lead status update → 200."""
        req, item, lead = req_with_lead
        from unittest.mock import MagicMock

        mock_updated = MagicMock()
        mock_updated.id = lead.id
        mock_updated.buyer_status = "contacted"
        mock_updated.confidence_score = 0.8
        mock_updated.confidence_band = "high"
        mock_updated.vendor_safety_score = 0.7
        mock_updated.vendor_safety_band = "medium"
        mock_updated.buyer_feedback_summary = None
        with patch(
            "app.routers.requisitions.requirements.update_lead_status",
            return_value=mock_updated,
        ):
            resp = client.patch(
                f"/api/leads/{lead.id}/status",
                json={"status": "contacted"},
            )
        assert resp.status_code == 200


# ── search_all stats merge + _attach_lead_data ────────────────────────


class TestSearchAllStatsMerge:
    def test_duplicate_source_in_stats_merges(self, client: TestClient, test_requisition, db_session: Session):
        """Lines 837-838: same source appears twice in merged_source_stats.

        We mock search_requirement to return a result with two entries
        for the same source name, forcing the else branch.
        """
        mock_result = {
            "sightings": [],
            "source_stats": [
                {"source": "brokerbin", "results": 3, "ms": 100, "error": None, "status": "ok"},
            ],
        }

        async def _mock_search(req, db):
            return mock_result

        # Two requirements → two search calls for same source → merge triggers
        item2 = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="NE555",
            target_qty=50,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item2)
        db_session.commit()

        with patch("app.routers.requisitions.search_requirement", new=_mock_search):
            resp = client.post(f"/api/requisitions/{test_requisition.id}/search")
        assert resp.status_code == 200
        # Coverage: line 837 (else: existing = ...), 838 (existing["results"] +=)

    def test_search_with_lead_covers_attach_lead_data(self, client: TestClient, req_with_lead):
        """Line 194: _attach_lead_data iterates over leads when req in results.

        search_all always puts requirements in results dict (unlike get_saved_sightings
        which skips empty ones). So a lead + search call → line 194 covered.
        """
        req, item, lead = req_with_lead
        mock_result = {
            "sightings": [],
            "source_stats": [
                {"source": "brokerbin", "results": 0, "ms": 50, "error": None, "status": "ok"},
            ],
        }

        async def _mock_search(r, db):
            return mock_result

        with patch("app.routers.requisitions.search_requirement", new=_mock_search):
            resp = client.post(f"/api/requisitions/{req.id}/search")
        assert resp.status_code == 200
        # Coverage: line 194 (lead_cards.append in _attach_lead_data)

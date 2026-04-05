"""test_requirements_async_coverage.py — Tests for async requirements router endpoints.

Covers missing lines in app/routers/requisitions/requirements.py including:
- add_requirements route (batch + single, validation failures)
- search_requirements 404 path
- get_cached_sightings 404 path
- stock import route
- list_requirements 404 path

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_requisition)
"""

import os

os.environ["TESTING"] = "1"

from unittest.mock import patch

from app.models import Requirement

# ── list_requirements 404 ─────────────────────────────────────────────


def test_list_requirements_req_not_found(client):
    with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
        resp = client.get("/api/requisitions/99999/requirements")
    assert resp.status_code == 404


# ── add_requirements ──────────────────────────────────────────────────


class TestAddRequirements:
    def test_req_not_found(self, client):
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.post(
                "/api/requisitions/99999/requirements",
                json={"primary_mpn": "LM317T", "manufacturer": "TI"},
            )
        assert resp.status_code == 404

    def test_single_requirement_success(self, client, db_session, test_user, test_requisition):
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json={"primary_mpn": "LM317T", "manufacturer": "Texas Instruments", "target_qty": 100},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("created", [])) >= 1

    def test_batch_requirements_success(self, client, test_requisition):
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json=[
                    {"primary_mpn": "BC547", "manufacturer": "TI", "target_qty": 50},
                    {"primary_mpn": "2N3904", "manufacturer": "Fairchild", "target_qty": 25},
                ],
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("created", [])) >= 1

    def test_batch_with_invalid_item_skips(self, client, test_requisition):
        """Batch mode skips invalid items instead of raising 422."""
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json=[
                    {"primary_mpn": "LM317T", "manufacturer": "TI"},
                    {"primary_mpn": "", "manufacturer": "TI"},  # Invalid - blank MPN
                ],
            )
        assert resp.status_code == 200
        data = resp.json()
        # The valid one was created, the invalid one was skipped
        assert "skipped" in data or len(data.get("created", [])) >= 1

    def test_single_invalid_item_returns_422(self, client, test_requisition):
        """Single mode raises 422 on validation failure."""
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/requirements",
                json={"primary_mpn": "", "manufacturer": "TI"},
            )
        assert resp.status_code == 422


# ── search_requirements ───────────────────────────────────────────────


def test_search_requirements_req_not_found(client):
    with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
        resp = client.post(
            "/api/requisitions/99999/search",
            json={},
        )
    assert resp.status_code == 404


def test_search_requirements_with_exception(client, test_requisition, db_session):

    r = Requirement(
        requisition_id=test_requisition.id,
        primary_mpn="LM317T",
        manufacturer="TI",
        target_qty=10,
    )
    db_session.add(r)
    db_session.commit()

    async def _fail(*args, **kwargs):
        raise Exception("Search failed")

    with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
        with patch("app.search_service.search_requirement", side_effect=_fail):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/search",
                json={},
            )
    assert resp.status_code == 200


def test_search_requirements_draft_to_active_transition(client, test_requisition, db_session):
    """Tests the draft → active transition during search (lines 841-847)."""
    from app.constants import RequisitionStatus

    test_requisition.status = RequisitionStatus.DRAFT

    async def _search(*args, **kwargs):
        return {"sightings": [], "source_stats": []}

    with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
        with patch("app.routers.requisitions.__init__.search_requirement", side_effect=_search):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/search",
                json={},
            )
    assert resp.status_code == 200


# ── get_cached_sightings ──────────────────────────────────────────────


def test_get_cached_sightings_req_not_found(client):
    with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
        resp = client.get("/api/requisitions/99999/sightings/cached")
    assert resp.status_code == 404


# ── list_requisition_leads ────────────────────────────────────────────


def test_list_requisition_leads_req_not_found(client):
    with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
        resp = client.get("/api/requisitions/99999/leads")
    assert resp.status_code == 404


def test_list_requisition_leads_success(client, test_requisition):
    with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
        with patch("app.routers.requisitions.requirements.get_requisition_leads", return_value=[]):
            resp = client.get(f"/api/requisitions/{test_requisition.id}/leads")
    assert resp.status_code == 200


# ── stock import ──────────────────────────────────────────────────────


class TestStockImport:
    def test_import_req_not_found(self, client):
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=None):
            resp = client.post(
                "/api/requisitions/99999/import-stock",
                files={"file": ("test.csv", b"mpn,qty\nLM317T,100", "text/csv")},
                data={"vendor_name": "Test Vendor"},
            )
        assert resp.status_code == 404

    def test_import_no_file(self, client, test_requisition):
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                data={"vendor_name": "Test Vendor"},
            )
        assert resp.status_code in (400, 422)

    def test_import_csv_no_matches(self, client, db_session, test_requisition):
        """Upload CSV but no requirement MPNs match."""
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                files={"file": ("stock.csv", b"mpn,qty,price\nXYZ999,100,1.50", "text/csv")},
                data={"vendor_name": "Test Vendor"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_sightings"] == 0

    def test_import_csv_exceeds_size_limit(self, client, test_requisition):
        """Very large file returns 413."""
        large_content = b"x" * (10_000_001)
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                files={"file": ("stock.csv", large_content, "text/csv")},
                data={"vendor_name": "Test Vendor"},
            )
        assert resp.status_code == 413

    def test_import_csv_matching_requirement(self, client, db_session, test_requisition):
        """Upload CSV with matching requirement MPN."""
        r = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            target_qty=100,
        )
        db_session.add(r)
        db_session.commit()

        csv_content = b"mpn,qty,price,condition\nLM317T,100,1.25,new"
        with patch("app.routers.requisitions.requirements.get_req_for_user", return_value=test_requisition):
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/import-stock",
                files={"file": ("stock.csv", csv_content, "text/csv")},
                data={"vendor_name": "Arrow Electronics"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_sightings"] >= 1

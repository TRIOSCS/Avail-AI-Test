"""tests/test_requisitions_core_coverage.py — Coverage for app/routers/requisitions/core.py.

Targets uncovered branches:
- requisition_counts with SALES role (line 62)
- list_requisitions with multi-status filter, search, archive status filter
- get_requisition 404
- mark_outcome 404
- update_requisition with urgency validation, opportunity_value, etc.
- claim/unclaim (role checks, 404, ValueError)
- batch operations

Called by: pytest
Depends on: conftest.py fixtures
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import RequisitionStatus, UserRole
from app.models import Requisition, User


# ── Requisition Counts ───────────────────────────────────────────────


class TestRequisitionCounts:
    def test_counts_as_buyer(self, client, test_requisition):
        """GET /api/requisitions/counts returns counts for buyer role."""
        resp = client.get("/api/requisitions/counts")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "open" in data
        assert "archive" in data

    def test_counts_as_sales_user(self, db_session):
        """Sales user only counts own requisitions."""
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_user
        from app.main import app

        sales = User(
            email="salescounts@test.com",
            name="Sales Counts",
            role="sales",
            azure_id="sc-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sales)
        db_session.commit()

        # Create req by sales user
        req = Requisition(
            name="Sales Req",
            customer_name="Test Co",
            status="active",
            created_by=sales.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        def override_db():
            yield db_session

        def override_sales():
            return sales

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_user] = override_sales
        app.dependency_overrides[require_admin] = override_sales
        app.dependency_overrides[require_buyer] = override_sales

        from fastapi.testclient import TestClient

        try:
            with TestClient(app) as c:
                resp = c.get("/api/requisitions/counts")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total"] >= 1
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer]:
                app.dependency_overrides.pop(dep, None)


# ── List Requisitions ────────────────────────────────────────────────


class TestListRequisitions:
    def test_list_defaults(self, client, test_requisition):
        """GET /api/requisitions returns list with items and total."""
        resp = client.get("/api/requisitions")
        assert resp.status_code == 200
        data = resp.json()
        assert "requisitions" in data or "items" in data

    def test_list_with_search_query(self, client, test_requisition):
        """GET /api/requisitions?q=test searches by name."""
        resp = client.get("/api/requisitions?q=REQ-TEST")
        assert resp.status_code == 200

    def test_list_with_archive_status(self, client, db_session, test_user):
        """GET /api/requisitions?status=archive returns archived reqs."""
        archived = Requisition(
            name="Archived Req",
            customer_name="Test Co",
            status=RequisitionStatus.ARCHIVED,
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(archived)
        db_session.commit()

        resp = client.get("/api/requisitions?status=archive")
        assert resp.status_code == 200

    def test_list_with_single_status(self, client, test_requisition):
        """GET /api/requisitions?status=active filters to single status."""
        resp = client.get("/api/requisitions?status=active")
        assert resp.status_code == 200

    def test_list_with_multiple_statuses(self, client, test_requisition):
        """GET /api/requisitions?status=active,draft filters to multiple statuses."""
        resp = client.get("/api/requisitions?status=active,draft")
        assert resp.status_code == 200

    def test_list_sort_asc(self, client, test_requisition):
        """GET /api/requisitions with sort=name&order=asc works."""
        resp = client.get("/api/requisitions?sort=name&order=asc")
        assert resp.status_code == 200

    def test_list_sort_invalid_defaults_to_created_at(self, client, test_requisition):
        """Invalid sort column falls back to created_at."""
        resp = client.get("/api/requisitions?sort=invalid_col")
        assert resp.status_code == 200

    def test_list_with_limit_offset(self, client, test_requisition):
        """GET /api/requisitions with limit/offset returns paginated results."""
        resp = client.get("/api/requisitions?limit=10&offset=0")
        assert resp.status_code == 200


# ── Get Requisition ──────────────────────────────────────────────────


class TestGetRequisition:
    def test_get_existing_requisition(self, client, test_requisition):
        """GET /api/requisitions/{id} returns requisition data."""
        resp = client.get(f"/api/requisitions/{test_requisition.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == test_requisition.id
        assert data["name"] == test_requisition.name

    def test_get_nonexistent_requisition_returns_404(self, client):
        """GET /api/requisitions/99999 returns 404."""
        resp = client.get("/api/requisitions/99999")
        assert resp.status_code == 404

    def test_get_requisition_with_requirement_count(self, client, test_requisition):
        """Returned requisition includes requirement_count."""
        resp = client.get(f"/api/requisitions/{test_requisition.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "requirement_count" in data
        assert data["requirement_count"] >= 1


# ── Sourcing Score ───────────────────────────────────────────────────


class TestSourcingScore:
    def test_sourcing_score_not_found(self, client):
        """GET sourcing score for non-existent req returns 404."""
        resp = client.get("/api/requisitions/99999/sourcing-score")
        assert resp.status_code == 404

    def test_sourcing_score_returns_data(self, client, test_requisition):
        """GET sourcing score returns scoring data."""
        resp = client.get(f"/api/requisitions/{test_requisition.id}/sourcing-score")
        assert resp.status_code == 200


# ── Mark Outcome ─────────────────────────────────────────────────────


class TestMarkOutcome:
    def test_mark_outcome_not_found(self, client):
        """PUT outcome for non-existent req returns 404."""
        resp = client.put(
            "/api/requisitions/99999/outcome",
            json={"outcome": "won"},
        )
        assert resp.status_code == 404

    def test_mark_outcome_success(self, client, test_requisition):
        """PUT outcome marks requisition as won."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}/outcome",
            json={"outcome": "won"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── Update Requisition ───────────────────────────────────────────────


class TestUpdateRequisition:
    def test_update_not_found(self, client):
        """PUT /api/requisitions/99999 returns 404."""
        resp = client.put(
            "/api/requisitions/99999",
            json={"name": "Updated Name"},
        )
        assert resp.status_code == 404

    def test_update_name_strips_html(self, client, test_requisition):
        """Update name strips HTML tags."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}",
            json={"name": "<b>Bold Name</b>"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "Bold Name"

    def test_update_invalid_urgency_returns_400(self, client, test_requisition):
        """Update with invalid urgency returns 400."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}",
            json={"urgency": "super-urgent"},
        )
        assert resp.status_code == 400

    def test_update_valid_urgency(self, client, test_requisition):
        """Update with valid urgency works."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}",
            json={"urgency": "hot"},
        )
        assert resp.status_code == 200

    def test_update_opportunity_value(self, client, test_requisition):
        """Update opportunity_value field works."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}",
            json={"opportunity_value": 5000.00},
        )
        assert resp.status_code == 200

    def test_update_customer_site_id(self, client, test_requisition, test_customer_site):
        """Update customer_site_id works."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}",
            json={"customer_site_id": test_customer_site.id},
        )
        assert resp.status_code == 200

    def test_update_deadline(self, client, test_requisition):
        """Update deadline works."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}",
            json={"deadline": "2026-12-31"},
        )
        assert resp.status_code == 200

    def test_update_empty_deadline_sets_null(self, client, test_requisition):
        """Update with empty deadline sets it to None."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}",
            json={"deadline": ""},
        )
        assert resp.status_code == 200


# ── Toggle Archive ───────────────────────────────────────────────────


class TestToggleArchive:
    def test_archive_not_found(self, client):
        """PUT archive for non-existent req returns 404."""
        resp = client.put("/api/requisitions/99999/archive")
        assert resp.status_code == 404

    def test_archive_active_req(self, client, test_requisition):
        """Archiving an active req sets status to archived."""
        resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
        assert resp.status_code == 200
        assert resp.json()["status"] == "archived"

    def test_unarchive_archived_req(self, client, db_session, test_user):
        """Unarchiving an archived req sets status back to active."""
        req = Requisition(
            name="Archived to Restore",
            customer_name="Test Co",
            status=RequisitionStatus.ARCHIVED,
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        resp = client.put(f"/api/requisitions/{req.id}/archive")
        assert resp.status_code == 200
        assert resp.json()["status"] == RequisitionStatus.ACTIVE


# ── Bulk Archive ─────────────────────────────────────────────────────


class TestBulkArchive:
    def test_bulk_archive_returns_count(self, client, db_session, test_user):
        """PUT /api/requisitions/bulk-archive archives all non-owner active reqs."""
        other_user = User(
            email="other_bulk@test.com",
            name="Other Bulk",
            role="buyer",
            azure_id="bulk-other-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_user)
        db_session.flush()

        req = Requisition(
            name="Other Bulk Req",
            customer_name="Test Co",
            status=RequisitionStatus.ACTIVE,
            created_by=other_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        resp = client.put("/api/requisitions/bulk-archive")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["archived_count"] >= 1


# ── Batch Archive ────────────────────────────────────────────────────


class TestBatchArchive:
    def test_batch_archive_by_ids(self, client, test_requisition):
        """PUT /api/requisitions/batch-archive archives listed IDs."""
        resp = client.put(
            "/api/requisitions/batch-archive",
            json={"ids": [test_requisition.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_batch_archive_already_archived_ids(self, client, db_session, test_user):
        """Batch archive of already-archived req returns 0 archived."""
        req = Requisition(
            name="Already Archived",
            customer_name="Test Co",
            status=RequisitionStatus.ARCHIVED,
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()
        resp = client.put(
            "/api/requisitions/batch-archive",
            json={"ids": [req.id]},
        )
        assert resp.status_code == 200
        assert resp.json()["archived_count"] == 0

    def test_batch_archive_sales_role_own_reqs(self, db_session):
        """Sales user can only batch-archive own reqs."""
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_user
        from app.main import app

        sales = User(
            email="salesbatch@test.com",
            name="Sales Batch",
            role="sales",
            azure_id="sb-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sales)
        db_session.flush()

        req = Requisition(
            name="Sales Batch Req",
            customer_name="Test Co",
            status=RequisitionStatus.ACTIVE,
            created_by=sales.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        def override_db():
            yield db_session

        def override_sales():
            return sales

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_user] = override_sales
        app.dependency_overrides[require_admin] = override_sales
        app.dependency_overrides[require_buyer] = override_sales

        from fastapi.testclient import TestClient

        try:
            with TestClient(app) as c:
                resp = c.put("/api/requisitions/batch-archive", json={"ids": [req.id]})
            assert resp.status_code == 200
            assert resp.json()["archived_count"] >= 1
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer]:
                app.dependency_overrides.pop(dep, None)


# ── Batch Assign ─────────────────────────────────────────────────────


class TestBatchAssign:
    def test_batch_assign_user_not_found(self, client, test_requisition):
        """Batch assign to non-existent user returns 404."""
        resp = client.put(
            "/api/requisitions/batch-assign",
            json={"ids": [test_requisition.id], "owner_id": 99999},
        )
        assert resp.status_code == 404

    def test_batch_assign_success(self, client, db_session, test_requisition, test_user):
        """Batch assign to existing user returns count."""
        resp = client.put(
            "/api/requisitions/batch-assign",
            json={"ids": [test_requisition.id], "owner_id": test_user.id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["assigned_count"] >= 1


# ── Dismiss New Offers ────────────────────────────────────────────────


class TestDismissNewOffers:
    def test_dismiss_not_found(self, client):
        """POST dismiss-new-offers for non-existent req returns 404."""
        resp = client.post("/api/requisitions/99999/dismiss-new-offers")
        assert resp.status_code == 404

    def test_dismiss_success(self, client, test_requisition):
        """POST dismiss-new-offers marks offers as viewed."""
        resp = client.post(f"/api/requisitions/{test_requisition.id}/dismiss-new-offers")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── Claim/Unclaim ─────────────────────────────────────────────────────


class TestClaimRequisition:
    def test_claim_wrong_role_returns_403(self, db_session):
        """Non-buyer role cannot claim requisition."""
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_user
        from app.main import app

        sales = User(
            email="salesnoclaim@test.com",
            name="Sales No Claim",
            role="sales",
            azure_id="snc-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sales)
        db_session.flush()

        req = Requisition(
            name="Claim Test Req",
            customer_name="Test Co",
            status=RequisitionStatus.ACTIVE,
            created_by=sales.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.commit()

        def override_db():
            yield db_session

        def override_sales():
            return sales

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[require_user] = override_sales
        app.dependency_overrides[require_admin] = override_sales
        app.dependency_overrides[require_buyer] = override_sales

        from fastapi.testclient import TestClient

        try:
            with TestClient(app) as c:
                resp = c.post(f"/api/requisitions/{req.id}/claim")
            assert resp.status_code == 403
        finally:
            for dep in [get_db, require_user, require_admin, require_buyer]:
                app.dependency_overrides.pop(dep, None)

    def test_claim_req_not_found(self, client):
        """Claim non-existent requisition returns 404."""
        resp = client.post("/api/requisitions/99999/claim")
        assert resp.status_code == 404

    def test_claim_success(self, client, test_requisition, test_user, db_session):
        """Buyer can claim unclaimed requisition."""
        test_user.role = "buyer"
        db_session.commit()

        with patch("app.services.requirement_status.claim_requisition", return_value=True):
            resp = client.post(f"/api/requisitions/{test_requisition.id}/claim")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_claim_conflict_raises_409(self, client, test_requisition, test_user, db_session):
        """Claim conflict (already claimed) returns 409."""
        test_user.role = "buyer"
        db_session.commit()

        with patch(
            "app.services.requirement_status.claim_requisition",
            side_effect=ValueError("Already claimed"),
        ):
            resp = client.post(f"/api/requisitions/{test_requisition.id}/claim")
        assert resp.status_code == 409


class TestUnclaimRequisition:
    def test_unclaim_not_found(self, client):
        """Unclaim non-existent requisition returns 404."""
        resp = client.delete("/api/requisitions/99999/claim")
        assert resp.status_code == 404

    def test_unclaim_by_non_owner_returns_403(self, client, db_session, test_user, test_requisition):
        """Non-owner buyer cannot unclaim another's requisition."""
        other_user = User(
            email="other_claim@test.com",
            name="Other Claimer",
            role="buyer",
            azure_id="oc-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_user)
        db_session.flush()

        # Set claimed_by_id to other user
        test_requisition.claimed_by_id = other_user.id
        test_user.role = "buyer"
        db_session.commit()

        resp = client.delete(f"/api/requisitions/{test_requisition.id}/claim")
        assert resp.status_code == 403

    def test_unclaim_by_owner_succeeds(self, client, db_session, test_user, test_requisition):
        """Owner can unclaim their own requisition."""
        test_requisition.claimed_by_id = test_user.id
        db_session.commit()

        with patch("app.services.requirement_status.unclaim_requisition", return_value=True):
            resp = client.delete(f"/api/requisitions/{test_requisition.id}/claim")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

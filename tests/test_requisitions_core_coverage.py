"""tests/test_requisitions_core_coverage.py — Coverage for
app/routers/requisitions/core.py.

Targets uncovered branches:
- requisition_counts with SALES role (line 62)
- list_requisitions with multi-status filter, search
- get_requisition 404
- mark_outcome 404
- update_requisition with urgency validation, opportunity_value, etc.
- claim/unclaim (role checks, 404, ValueError)
- batch assign

Called by: pytest
Depends on: conftest.py fixtures
"""

import os

os.environ["TESTING"] = "1"

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.constants import RequisitionStatus
from app.models import Requisition, User

# ── Helpers ──────────────────────────────────────────────────────────


def _make_req(db_session, created_by, *, name="Req", status=RequisitionStatus.OPEN, **kw) -> Requisition:
    req = Requisition(
        name=name,
        customer_name="Test Co",
        status=status,
        created_by=created_by,
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db_session.add(req)
    return req


@contextmanager
def _client_as(db_session, user):
    """Yield a TestClient whose auth/role dependencies all resolve to ``user``."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_user
    from app.main import app

    def override_db():
        yield db_session

    def override_user():
        return user

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[require_user] = override_user
    app.dependency_overrides[require_admin] = override_user
    app.dependency_overrides[require_buyer] = override_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_user, require_admin, require_buyer]:
            app.dependency_overrides.pop(dep, None)


# ── Requisition Counts ───────────────────────────────────────────────


class TestRequisitionCounts:
    def test_counts_as_buyer(self, client, test_requisition):
        """GET /api/requisitions/counts returns counts for buyer role."""
        resp = client.get("/api/requisitions/counts")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "open" in data

    def test_counts_as_sales_user(self, db_session):
        """Sales user only counts own requisitions."""
        sales = User(
            email="salescounts@test.com",
            name="Sales Counts",
            role="sales",
            azure_id="sc-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sales)
        db_session.commit()

        _make_req(db_session, sales.id, name="Sales Req", status="open")
        db_session.commit()

        with _client_as(db_session, sales) as c:
            resp = c.get("/api/requisitions/counts")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1


# ── List Requisitions ────────────────────────────────────────────────


class TestListRequisitions:
    def test_list_defaults(self, client, test_requisition):
        """GET /api/requisitions returns list with items and total."""
        resp = client.get("/api/requisitions")
        assert resp.status_code == 200
        data = resp.json()
        assert "requisitions" in data or "items" in data

    @pytest.mark.parametrize(
        "query",
        [
            pytest.param("?q=REQ-TEST", id="search_query"),
            pytest.param("?status=open", id="single_status"),
            pytest.param("?status=open,rfqs_sent", id="multiple_statuses"),
            pytest.param("?sort=name&order=asc", id="sort_asc"),
            pytest.param("?sort=invalid_col", id="sort_invalid_defaults_to_created_at"),
            pytest.param("?limit=10&offset=0", id="limit_offset"),
        ],
    )
    def test_list_with_query_params(self, client, test_requisition, query):
        """Filtering/sorting/pagination query params all return 200."""
        resp = client.get(f"/api/requisitions{query}")
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
        """PUT outcome marks requisition as won (a close reason is required)."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}/outcome",
            json={"outcome": "won", "reason": "Customer signed PO"},
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

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"urgency": "hot"}, id="valid_urgency"),
            pytest.param({"opportunity_value": 5000.00}, id="opportunity_value"),
            pytest.param({"deadline": "2026-12-31"}, id="deadline"),
            pytest.param({"deadline": ""}, id="empty_deadline_sets_null"),
        ],
    )
    def test_update_field_succeeds(self, client, test_requisition, body):
        """Valid single-field updates return 200."""
        resp = client.put(f"/api/requisitions/{test_requisition.id}", json=body)
        assert resp.status_code == 200

    def test_update_customer_site_id(self, client, test_requisition, test_customer_site):
        """Update customer_site_id works."""
        resp = client.put(
            f"/api/requisitions/{test_requisition.id}",
            json={"customer_site_id": test_customer_site.id},
        )
        assert resp.status_code == 200


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

    def test_batch_assign_returns_ids(self, client, db_session, test_user):
        """Batch-assign response includes assigned_ids matching the requested IDs."""
        target = User(
            email="assign_ids_target@test.com",
            name="Assign IDs Target",
            role="buyer",
            azure_id="assign-ids-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(target)
        db_session.flush()

        reqs = [_make_req(db_session, test_user.id, name=f"Assign IDs Req {i}") for i in range(3)]
        db_session.commit()
        ids = [r.id for r in reqs]

        resp = client.put(
            "/api/requisitions/batch-assign",
            json={"ids": ids, "owner_id": target.id},
        )
        assert resp.status_code == 200
        body = resp.json()

        assert sorted(body["assigned_ids"]) == sorted(ids)
        assert body["assigned_count"] == 3


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
        sales = User(
            email="salesnoclaim@test.com",
            name="Sales No Claim",
            role="sales",
            azure_id="snc-001",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(sales)
        db_session.flush()

        req = _make_req(db_session, sales.id, name="Claim Test Req")
        db_session.commit()

        with _client_as(db_session, sales) as c:
            resp = c.post(f"/api/requisitions/{req.id}/claim")
        assert resp.status_code == 403

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

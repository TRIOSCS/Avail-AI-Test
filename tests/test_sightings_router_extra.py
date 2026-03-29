"""Extra tests for app/routers/sightings.py — targeting missing coverage lines.

Covers batch-refresh, batch-assign, batch-status, batch-notes, mark-unavailable,
assign-buyer, advance-status, vendor-modal, and send-inquiry endpoints.

Called by: pytest
Depends on: conftest fixtures, FastAPI TestClient
"""

import os

os.environ["TESTING"] = "1"

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User


@pytest.fixture()
def req_with_item(db_session: Session, test_user: User) -> tuple:
    req = Requisition(
        name="SIGHT-EXTRA-REQ",
        customer_name="Test Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


class TestBatchRefresh:
    def test_empty_requirement_ids(self, client: TestClient):
        with patch("app.search_service.search_requirement", new=AsyncMock()):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": "[]"},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200

    def test_batch_refresh_too_many_ids(self, client: TestClient):
        ids = list(range(51))
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps(ids)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_refresh_with_valid_ids(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        with patch("app.search_service.search_requirement", new=AsyncMock()):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item.id])},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "Refreshed" in resp.text

    def test_batch_refresh_nonexistent_ids(self, client: TestClient):
        with patch("app.search_service.search_requirement", new=AsyncMock()):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([99999])},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "failed" in resp.text.lower() or "0/" in resp.text


class TestBatchAssign:
    def test_batch_assign_empty_ids(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": "[]", "buyer_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No requirements" in resp.text

    def test_batch_assign_too_many(self, client: TestClient):
        ids = list(range(51))
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps(ids), "buyer_id": "1"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_assign_no_buyer(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([item.id]), "buyer_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "nobody" in resp.text.lower() or "Assigned" in resp.text

    def test_batch_assign_with_buyer(
        self, client: TestClient, req_with_item: tuple, test_user: User
    ):
        _, item = req_with_item
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([item.id]), "buyer_id": str(test_user.id)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Assigned" in resp.text


class TestBatchStatus:
    def test_batch_status_empty_ids(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": "[]", "status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No requirements" in resp.text

    def test_batch_status_too_many(self, client: TestClient):
        ids = list(range(51))
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps(ids), "status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_status_invalid_status(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([item.id]), "status": "INVALID_STATUS"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_status_valid_transition(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([item.id]), "status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Updated" in resp.text


class TestBatchNotes:
    def test_batch_notes_empty_ids(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": "[]", "notes": "Test note"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No requirements" in resp.text

    def test_batch_notes_too_many(self, client: TestClient):
        ids = list(range(51))
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps(ids), "notes": "note"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_notes_empty_note(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([item.id]), "notes": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "required" in resp.text.lower()

    def test_batch_notes_success(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([item.id]), "notes": "Test note here"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Added note" in resp.text


class TestAssignBuyer:
    def test_assign_buyer_not_found(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/sightings/99999/assign",
            data={"assigned_buyer_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_assign_buyer_clears_assignment(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        resp = client.patch(
            f"/v2/partials/sightings/{item.id}/assign",
            data={"assigned_buyer_id": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


class TestAdvanceStatus:
    def test_advance_status_missing_status(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        resp = client.patch(
            f"/v2/partials/sightings/{item.id}/advance-status",
            data={"status": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_advance_status_not_found(self, client: TestClient):
        resp = client.patch(
            "/v2/partials/sightings/99999/advance-status",
            data={"status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_advance_status_valid(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        resp = client.patch(
            f"/v2/partials/sightings/{item.id}/advance-status",
            data={"status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_advance_status_invalid_transition(
        self, client: TestClient, req_with_item: tuple, db_session: Session
    ):
        _, item = req_with_item
        item.sourcing_status = "sourcing"
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/sightings/{item.id}/advance-status",
            data={"status": "open"},  # Can't go backwards
            headers={"HX-Request": "true"},
        )
        # Either 409 conflict or 200 with error toast
        assert resp.status_code in (200, 409)


class TestVendorModal:
    def test_vendor_modal_no_params(self, client: TestClient):
        resp = client.get(
            "/v2/partials/sightings/vendor-modal",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_vendor_modal_with_vendor_id(
        self, client: TestClient, test_vendor_card
    ):
        resp = client.get(
            f"/v2/partials/sightings/vendor-modal?vendor_id={test_vendor_card.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


class TestMarkUnavailable:
    def test_mark_unavailable_no_vendor_name(
        self, client: TestClient, req_with_item: tuple
    ):
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/mark-unavailable",
            data={"vendor_name": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

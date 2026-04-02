"""tests/test_coverage_boost_attachments.py — Coverage for uncovered error paths
in app/routers/requisitions/attachments.py that execute before any await.

Targets:
  - line 39:  list_requisition_attachments → 404 for missing req
  - line 64:  upload_requisition_attachment → 404 for missing req
  - line 123: attach_requisition_from_onedrive → 404 for missing req
  - line 215: list_requirement_attachments → 404 for missing requirement

Called by: pytest
Depends on: tests/conftest.py (client, db_session, test_user)
"""

import os

os.environ["TESTING"] = "1"

from fastapi.testclient import TestClient


class TestListRequisitionAttachments:
    def test_missing_req_returns_404(self, client: TestClient):
        """Line 39: get_req_for_user returns None → 404."""
        resp = client.get("/api/requisitions/999999/attachments")
        assert resp.status_code == 404

    def test_valid_req_returns_list(self, client: TestClient, test_requisition):
        """Happy path: valid req with no attachments → empty list."""
        resp = client.get(f"/api/requisitions/{test_requisition.id}/attachments")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestUploadRequisitionAttachment:
    def test_missing_req_returns_404(self, client: TestClient):
        """Line 64: get_req_for_user returns None → 404 before any await."""
        resp = client.post(
            "/api/requisitions/999999/attachments",
            files={"file": ("test.txt", b"hello", "text/plain")},
        )
        assert resp.status_code == 404


class TestAttachFromOnedrive:
    def test_missing_req_returns_404(self, client: TestClient):
        """Line 123: get_req_for_user returns None → 404 before any await."""
        resp = client.post(
            "/api/requisitions/999999/attachments/onedrive",
            json={"item_id": "fake-item-id"},
        )
        assert resp.status_code == 404


class TestListRequirementAttachments:
    def test_missing_requirement_returns_404(self, client: TestClient):
        """Line 215: db.get(Requirement, req_id) is None → 404."""
        resp = client.get("/api/requirements/999999/attachments")
        assert resp.status_code == 404

    def test_valid_requirement_returns_list(self, client: TestClient, test_requisition):
        """Happy path: valid requirement with no attachments → empty list."""
        # test_requisition fixture creates one requirement

        resp = client.get(f"/api/requirements/{test_requisition.requirements[0].id}/attachments")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

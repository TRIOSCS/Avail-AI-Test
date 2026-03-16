"""tests/test_htmx_req_inline.py — Tests for requisition inline editing endpoints.

Covers inline edit cell rendering, inline save (PATCH), and row actions.

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, test_requisition)
"""

from fastapi.testclient import TestClient

from app.models import Requisition, User


class TestInlineEditCell:
    """GET /v2/partials/requisitions/{id}/edit/{field} returns an edit form."""

    def test_edit_name_returns_input(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/edit/name?context=row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert 'name="value"' in resp.text
        assert 'name="field"' in resp.text
        assert test_requisition.name in resp.text

    def test_edit_status_returns_select(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/edit/status?context=header",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "<select" in resp.text
        assert "active" in resp.text
        assert "archived" in resp.text

    def test_edit_urgency_returns_select(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/edit/urgency?context=row",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "<select" in resp.text
        assert "normal" in resp.text.lower()
        assert "critical" in resp.text.lower()

    def test_edit_deadline_returns_date_input(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/edit/deadline?context=header",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert 'type="date"' in resp.text

    def test_edit_invalid_field_returns_400(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(
            f"/v2/partials/requisitions/{test_requisition.id}/edit/invalid_field",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_edit_nonexistent_req_returns_404(self, client: TestClient):
        resp = client.get(
            "/v2/partials/requisitions/99999/edit/name",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


class TestInlineSave:
    """PATCH /v2/partials/requisitions/{id}/inline saves and returns updated content."""

    def test_save_name_updates_requisition(
        self, client: TestClient, test_requisition: Requisition, db_session
    ):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "name", "value": "New Name", "context": "row"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "New Name" in resp.text
        db_session.refresh(test_requisition)
        assert test_requisition.name == "New Name"

    def test_save_urgency_updates_requisition(
        self, client: TestClient, test_requisition: Requisition, db_session
    ):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "urgency", "value": "critical", "context": "row"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.urgency == "critical"

    def test_save_deadline_updates_requisition(
        self, client: TestClient, test_requisition: Requisition, db_session
    ):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "deadline", "value": "2026-04-01", "context": "header"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.deadline == "2026-04-01"

    def test_save_header_context_returns_header_html(
        self, client: TestClient, test_requisition: Requisition
    ):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "name", "value": "Updated Header", "context": "header"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "req-header" in resp.text
        assert "Updated Header" in resp.text

    def test_save_returns_toast_trigger(
        self, client: TestClient, test_requisition: Requisition
    ):
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "name", "value": "Toast Test", "context": "row"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "showToast" in resp.headers.get("HX-Trigger", "")

    def test_save_empty_name_keeps_original(
        self, client: TestClient, test_requisition: Requisition, db_session
    ):
        original_name = test_requisition.name
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/inline",
            data={"field": "name", "value": "  ", "context": "row"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.name == original_name


class TestRowActions:
    """POST /v2/partials/requisitions/{id}/action/{action} executes row actions."""

    def test_archive_action(self, client: TestClient, test_requisition: Requisition, db_session):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/archive",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.status == "archived"

    def test_clone_action(self, client: TestClient, test_requisition: Requisition, db_session):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/clone",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        clones = db_session.query(Requisition).filter(Requisition.cloned_from_id == test_requisition.id).all()
        assert len(clones) == 1

    def test_claim_action(self, client: TestClient, test_requisition: Requisition, db_session):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/claim",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.claimed_by_id is not None

    def test_invalid_action_returns_400(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(
            f"/v2/partials/requisitions/{test_requisition.id}/action/destroy",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_nonexistent_req_returns_404(self, client: TestClient):
        resp = client.post(
            "/v2/partials/requisitions/99999/action/archive",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

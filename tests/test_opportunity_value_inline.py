"""Tests for the requisition opportunity_value (Deal $ Value) inline edit.

Mirrors the win_probability inline-edit tests. Covers the PATCH route
/v2/partials/requisitions/{req_id}/opportunity-value:
- valid set persists as Decimal and renders formatted ($1,500)
- empty form clears to NULL (200, not 400)
- negative -> 400; non-numeric -> 400
- non-owner SALES (restricted role) -> 404
- owner/admin -> 200

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_requisition, client)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Requisition, User


class TestOpportunityValueRoute:
    """Integration tests for PATCH /v2/partials/requisitions/{req_id}/opportunity-
    value."""

    def test_set_opportunity_value_persists(self, client, db_session: Session, test_requisition: Requisition):
        """Authorized owner can set opportunity_value and it persists as Decimal."""
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/opportunity-value",
            data={"opportunity_value": "1500"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.opportunity_value == Decimal("1500")

    def test_opportunity_value_displayed_in_response(self, client, db_session: Session, test_requisition: Requisition):
        """Response HTML shows the new deal value formatted as $1,500."""
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/opportunity-value",
            data={"opportunity_value": "1500"},
        )
        assert resp.status_code == 200
        assert "$1,500" in resp.text

    def test_opportunity_value_empty_clears_to_none(self, client, db_session: Session, test_requisition: Requisition):
        """Submitting an empty opportunity_value clears the value to NULL (200, not
        400)."""
        test_requisition.opportunity_value = Decimal("2500")
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/opportunity-value",
            data={"opportunity_value": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.opportunity_value is None

    def test_opportunity_value_negative_returns_400(self, client, db_session: Session, test_requisition: Requisition):
        """Negative opportunity_value returns 400 error."""
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/opportunity-value",
            data={"opportunity_value": "-5"},
        )
        assert resp.status_code == 400

    def test_opportunity_value_non_numeric_returns_400(
        self, client, db_session: Session, test_requisition: Requisition
    ):
        """Non-numeric opportunity_value returns 400 error."""
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/opportunity-value",
            data={"opportunity_value": "abc"},
        )
        assert resp.status_code == 400

    def test_opportunity_value_owner_returns_200(self, client, db_session: Session, test_requisition: Requisition):
        """The default client (owner buyer) gets 200 — confirms owner/admin access."""
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/opportunity-value",
            data={"opportunity_value": "999"},
        )
        assert resp.status_code == 200

    def test_opportunity_value_non_owner_sales_returns_404(
        self,
        db_session: Session,
        test_requisition: Requisition,
        sales_user: User,
    ):
        """A SALES (restricted) user who doesn't own the requisition gets 404."""
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        # test_requisition.created_by is test_user (a different user) — sales_user
        # is a non-owner restricted role, so require_requisition_access raises 404.
        def _override_db():
            yield db_session

        def _override_user():
            return sales_user

        async def _override_token():
            return "mock-token"

        overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = _override_user
        app.dependency_overrides[require_admin] = _override_user
        app.dependency_overrides[require_buyer] = _override_user
        app.dependency_overrides[require_fresh_token] = _override_token
        try:
            with TestClient(app, raise_server_exceptions=False) as c:
                resp = c.patch(
                    f"/v2/partials/requisitions/{test_requisition.id}/opportunity-value",
                    data={"opportunity_value": "1500"},
                )
                assert resp.status_code == 404
        finally:
            for dep in overridden:
                app.dependency_overrides.pop(dep, None)

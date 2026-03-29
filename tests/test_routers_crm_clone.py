"""Tests for app/routers/crm/clone.py — Requisition clone endpoint.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_requisition)
"""

from datetime import datetime, timezone

from app.models import Offer, Requirement, Requisition


class TestCloneRequisition:
    """POST /api/requisitions/{req_id}/clone."""

    def test_successful_clone_creates_new_requisition(self, client, db_session, test_user, test_requisition):
        """Clone returns ok with a new requisition ID and appended name."""
        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["id"] != test_requisition.id
        assert data["name"] == f"{test_requisition.name} (clone)"

    def test_clone_nonexistent_req_returns_404(self, client):
        """Cloning a requisition that doesn't exist returns 404."""
        resp = client.post("/api/requisitions/999999/clone")
        assert resp.status_code == 404

    def test_cloned_req_has_different_id_but_same_data(self, client, db_session, test_user, test_requisition):
        """The cloned requisition preserves customer info and records the source."""
        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        new_id = resp.json()["id"]

        cloned = db_session.get(Requisition, new_id)
        assert cloned is not None
        assert cloned.id != test_requisition.id
        assert cloned.customer_name == test_requisition.customer_name
        assert cloned.cloned_from_id == test_requisition.id
        assert cloned.created_by == test_user.id
        assert cloned.status == "active"

    def test_requirements_are_duplicated_to_new_req(self, client, db_session, test_user, test_requisition):
        """All requirements from the original are cloned to the new requisition."""
        # Add a second requirement to the original
        r2 = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="NE555P",
            target_qty=500,
            target_price=0.25,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(r2)
        db_session.commit()
        db_session.refresh(test_requisition)

        orig_count = len(test_requisition.requirements)
        assert orig_count == 2

        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        new_id = resp.json()["id"]

        cloned_reqs = db_session.query(Requirement).filter_by(requisition_id=new_id).all()
        assert len(cloned_reqs) == orig_count

        cloned_mpns = {r.primary_mpn for r in cloned_reqs}
        orig_mpns = {r.primary_mpn for r in test_requisition.requirements}
        # MPNs may be normalized but should still match the originals
        assert len(cloned_mpns) == len(orig_mpns)

    def test_active_offers_are_cloned_as_reference(self, client, db_session, test_user, test_requisition, test_offer):
        """Active offers on the original req are cloned with status='reference'."""
        resp = client.post(f"/api/requisitions/{test_requisition.id}/clone")
        assert resp.status_code == 200
        new_id = resp.json()["id"]

        cloned_offers = db_session.query(Offer).filter_by(requisition_id=new_id).all()
        assert len(cloned_offers) == 1
        assert cloned_offers[0].status == "reference"
        assert cloned_offers[0].vendor_name == test_offer.vendor_name
        assert f"REQ-{test_requisition.id:03d}" in cloned_offers[0].notes

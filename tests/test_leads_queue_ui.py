"""Tests for the cross-req buyer-leads queue UI (Phase-4 cleanup Task 4 / B1).

Covers: GET /v2/partials/leads/queue (creator-scope isolation, requisition/requirement
grouping headers, buyer_status chip filter), the full-page GET /v2/leads/queue route,
and the Queues-strip mount anchor in the Sightings workspace partial. The JSON endpoint
(/api/leads/queue, requirements.py) and its own tests are untouched — this suite pins
the HTML surface only.

Called by: pytest
Depends on: conftest fixtures (db_session, test_user, test_requisition, client)
"""

from datetime import UTC, datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User
from app.models.sourcing_lead import SourcingLead

_LEAD_SEQ = [0]


def _make_requirement(db: Session, req: Requisition, mpn: str = "LM317T", **kw) -> Requirement:
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=mpn.lower().replace("-", "").replace(" ", ""),
        target_qty=100,
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _make_lead(db: Session, item: Requirement, requisition: Requisition, vendor: str = "Arrow", **kw) -> SourcingLead:
    _LEAD_SEQ[0] += 1
    lead = SourcingLead(
        lead_id=f"LQ-{_LEAD_SEQ[0]}",
        requirement_id=item.id,
        requisition_id=requisition.id,
        part_number_requested=item.primary_mpn,
        part_number_matched=item.primary_mpn,
        vendor_name=vendor,
        vendor_name_normalized=vendor.lower(),
        primary_source_type="brokerbin",
        primary_source_name="BrokerBin",
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


class TestCreatorScopeIsolation:
    def test_other_users_requisition_leads_absent(self, client, db_session: Session, test_user: User, test_requisition):
        """A lead under a requisition created by a DIFFERENT user must never appear —
        mirrors the JSON endpoint's Requisition.created_by == user.id scoping
        exactly."""
        other_user = User(
            email="otherbuyer@trioscs.com",
            name="Other Buyer",
            role="buyer",
            azure_id="test-azure-id-other-999",
            created_at=datetime.now(UTC),
        )
        db_session.add(other_user)
        db_session.commit()

        other_req = Requisition(
            name="OTHER-REQ-999",
            customer_name="Other Customer",
            status="open",
            created_by=other_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(other_req)
        db_session.commit()
        other_item = _make_requirement(db_session, other_req, mpn="OTHERMPN")
        _make_lead(db_session, other_item, other_req, vendor="OtherVendorOnly")

        mine_item = db_session.query(Requirement).filter(Requirement.requisition_id == test_requisition.id).first()
        _make_lead(db_session, mine_item, test_requisition, vendor="MyOwnVendorLead")

        resp = client.get("/v2/partials/leads/queue?status=all")
        assert resp.status_code == 200
        assert "MyOwnVendorLead" in resp.text
        assert "OtherVendorOnly" not in resp.text
        assert "OTHER-REQ-999" not in resp.text


class TestGroupingHeaders:
    def test_groups_by_requisition_and_requirement_mpn(self, client, db_session: Session, test_user: User):
        req_a = Requisition(
            name="REQ-QUEUE-A",
            customer_name="Cust A",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(UTC),
        )
        req_b = Requisition(
            name="REQ-QUEUE-B",
            customer_name="Cust B",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add_all([req_a, req_b])
        db_session.commit()

        item_a = _make_requirement(db_session, req_a, mpn="MPN-AAA-1")
        item_b = _make_requirement(db_session, req_b, mpn="MPN-BBB-2")
        _make_lead(db_session, item_a, req_a, vendor="VendorForA")
        _make_lead(db_session, item_b, req_b, vendor="VendorForB")

        resp = client.get("/v2/partials/leads/queue?status=all")
        assert resp.status_code == 200
        html = resp.text
        assert "REQ-QUEUE-A" in html
        assert "REQ-QUEUE-B" in html
        assert "MPN-AAA-1" in html
        assert "MPN-BBB-2" in html
        assert "VendorForA" in html
        assert "VendorForB" in html


class TestStatusFilter:
    def test_filters_by_buyer_status_chip(self, client, db_session: Session, test_user: User, test_requisition):
        item = db_session.query(Requirement).filter(Requirement.requisition_id == test_requisition.id).first()
        _make_lead(db_session, item, test_requisition, vendor="NewStatusVendor", buyer_status="new")
        _make_lead(db_session, item, test_requisition, vendor="ContactedStatusVendor", buyer_status="contacted")

        resp_new = client.get("/v2/partials/leads/queue?status=new")
        assert resp_new.status_code == 200
        assert "NewStatusVendor" in resp_new.text
        assert "ContactedStatusVendor" not in resp_new.text

        resp_contacted = client.get("/v2/partials/leads/queue?status=contacted")
        assert "ContactedStatusVendor" in resp_contacted.text
        assert "NewStatusVendor" not in resp_contacted.text

        resp_all = client.get("/v2/partials/leads/queue?status=all")
        assert "NewStatusVendor" in resp_all.text
        assert "ContactedStatusVendor" in resp_all.text


class TestFullPageRoute:
    def test_full_page_renders(self, db_session: Session, test_user: User):
        """GET /v2/leads/queue (v2_page dispatcher) loads the app shell whose lazy
        partial_url resolves to /v2/partials/leads/queue.

        Full-page routes call get_user(request, db) directly rather than the
        require_user dependency, so it must be patched separately (mirrors the pattern
        in test_crm_views.py).
        """
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: test_user
        app.dependency_overrides[require_admin] = lambda: test_user
        app.dependency_overrides[require_buyer] = lambda: test_user
        app.dependency_overrides[require_fresh_token] = lambda: "mock-token"

        try:
            with patch("app.routers.htmx_views.get_user", return_value=test_user):
                with TestClient(app) as c:
                    resp = c.get("/v2/leads/queue")
        finally:
            for dep in (get_db, require_user, require_admin, require_buyer, require_fresh_token):
                app.dependency_overrides.pop(dep, None)

        assert resp.status_code == 200
        assert "/v2/partials/leads/queue" in resp.text


class TestQueuesStripMount:
    def test_leads_anchor_present_in_sightings_queues_strip(self, client):
        resp = client.get("/v2/partials/sightings/workspace")
        assert resp.status_code == 200
        assert 'hx-get="/v2/partials/leads/queue"' in resp.text
        assert 'hx-push-url="/v2/leads/queue"' in resp.text

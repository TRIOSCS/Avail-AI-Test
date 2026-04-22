"""tests/test_htmx_views_nightly16.py — Coverage for search, requirement update, poll-
inbox.

Targets:
  - poll_inbox_htmx
  - update_requirement (PUT)
  - search_form_partial
  - search_run (with/without mpn)
  - search_filter (cache miss)
  - search_lead_detail (various paths)
  - requisition_picker

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_requirement(db: Session, req: Requisition, mpn: str = "LM317T", **kw) -> Requirement:
    defaults = dict(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=10,
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(kw)
    r = Requirement(**defaults)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


# ── Poll Inbox ────────────────────────────────────────────────────────────


class TestPollInbox:
    def test_poll_success(self, client: TestClient, test_requisition: Requisition):
        resp = client.post(f"/v2/partials/requisitions/{test_requisition.id}/poll-inbox")
        assert resp.status_code == 200

    def test_req_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/requisitions/99999/poll-inbox")
        assert resp.status_code == 404


# ── Update Requirement ────────────────────────────────────────────────────


class TestUpdateRequirement:
    def test_update_success(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        item = _make_requirement(db_session, test_requisition, mpn="BC547")
        resp = client.put(
            f"/v2/partials/requisitions/{test_requisition.id}/requirements/{item.id}",
            data={
                "primary_mpn": "BC547",
                "manufacturer": "Fairchild",
                "target_qty": "200",
                "brand": "",
                "target_price": "",
                "substitutes": "",
                "customer_pn": "",
                "need_by_date": "",
                "condition": "new",
            },
        )
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.manufacturer == "Fairchild"

    def test_update_missing_manufacturer(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        item = _make_requirement(db_session, test_requisition)
        resp = client.put(
            f"/v2/partials/requisitions/{test_requisition.id}/requirements/{item.id}",
            data={"primary_mpn": "LM317T", "manufacturer": "", "target_qty": "10"},
        )
        assert resp.status_code == 422

    def test_update_req_not_found(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        item = _make_requirement(db_session, test_requisition)
        resp = client.put(
            f"/v2/partials/requisitions/99999/requirements/{item.id}",
            data={"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": "10"},
        )
        assert resp.status_code == 404

    def test_update_requirement_not_found(self, client: TestClient, test_requisition: Requisition):
        resp = client.put(
            f"/v2/partials/requisitions/{test_requisition.id}/requirements/99999",
            data={"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": "10"},
        )
        assert resp.status_code == 404

    def test_update_with_need_by_date(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        item = _make_requirement(db_session, test_requisition)
        resp = client.put(
            f"/v2/partials/requisitions/{test_requisition.id}/requirements/{item.id}",
            data={
                "primary_mpn": "LM317T",
                "manufacturer": "TI",
                "target_qty": "5",
                "need_by_date": "2026-12-31",
            },
        )
        assert resp.status_code == 200

    def test_update_with_invalid_date(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        item = _make_requirement(db_session, test_requisition)
        resp = client.put(
            f"/v2/partials/requisitions/{test_requisition.id}/requirements/{item.id}",
            data={
                "primary_mpn": "LM317T",
                "manufacturer": "TI",
                "target_qty": "5",
                "need_by_date": "not-a-date",
            },
        )
        assert resp.status_code == 200


# ── Search Form Partial ───────────────────────────────────────────────────


class TestSearchFormPartial:
    def test_get_search_form(self, client: TestClient):
        resp = client.get("/v2/partials/search")
        assert resp.status_code == 200


# ── Search Run ────────────────────────────────────────────────────────────


class TestSearchRun:
    def test_run_with_mpn(self, client: TestClient):
        resp = client.post("/v2/partials/search/run", data={"mpn": "NE555"})
        assert resp.status_code == 200

    def test_run_empty_mpn(self, client: TestClient):
        """Returns error HTML when no MPN provided."""
        resp = client.post("/v2/partials/search/run", data={"mpn": ""})
        assert resp.status_code == 200
        assert b"Please enter a part number" in resp.content

    def test_run_from_requirement(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        item = _make_requirement(db_session, test_requisition, mpn="LM317T")
        resp = client.post(
            f"/v2/partials/search/run?requirement_id={item.id}",
            data={"mpn": ""},
        )
        assert resp.status_code == 200

    def test_run_mpn_from_query_param(self, client: TestClient):
        resp = client.post("/v2/partials/search/run?mpn=TL071&requirement_id=0", data={})
        assert resp.status_code == 200


# ── Search Filter ─────────────────────────────────────────────────────────


class TestSearchFilter:
    def test_filter_cache_miss(self, client: TestClient):
        """With no Redis cache, returns 'Search results expired' message."""
        resp = client.get("/v2/partials/search/filter?search_id=nonexistent_id")
        assert resp.status_code == 200
        assert b"expired" in resp.content.lower()


# ── Search Lead Detail ────────────────────────────────────────────────────


class TestSearchLeadDetail:
    def test_no_mpn_no_search_id(self, client: TestClient):
        """No search_id, no mpn → returns 'No part number specified'."""
        resp = client.get("/v2/partials/search/lead-detail?idx=0")
        assert resp.status_code == 200
        assert b"No part number" in resp.content

    def test_cache_miss_with_search_id(self, client: TestClient):
        """search_id + vendor_key with empty cache → returns 'Lead not found'."""
        resp = client.get("/v2/partials/search/lead-detail?search_id=nosuch&vendor_key=acme&idx=0")
        assert resp.status_code == 200
        assert b"Lead not found" in resp.content


# ── Requisition Picker ────────────────────────────────────────────────────


class TestRequisitionPicker:
    def test_get_picker(self, client: TestClient, test_requisition: Requisition):
        resp = client.get("/v2/partials/search/requisition-picker?mpn=NE555")
        assert resp.status_code == 200

    def test_picker_with_items(self, client: TestClient):
        resp = client.get('/v2/partials/search/requisition-picker?mpn=BC547&items=[{"vendor_name":"Test"}]&action=add')
        assert resp.status_code == 200

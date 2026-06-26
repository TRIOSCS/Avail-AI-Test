"""tests/test_sightings_router_coverage2.py — Additional coverage for
app/routers/sightings.py.

Targets uncovered branches:
- sightings_refresh: refresh failed path
- sightings_list: filter branches (status, sales_person, assigned, q, group_by)
- sightings_detail: not found, various suggestion paths
- sightings_mark_unavailable: success path
- sightings_assign_buyer: with buyer id
- sightings_log_activity: all channels, invalid channel, empty notes
- sightings_preview_inquiry: success, missing params
- sightings_send_inquiry: success, missing params, failed send
- batch-refresh: failed path

Called by: pytest
Depends on: conftest.py fixtures
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

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def req_with_item(db_session: Session, test_user: User) -> tuple:
    req = Requisition(
        name="COV2-REQ",
        customer_name="Cov Co",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="STM32F4",
        target_qty=200,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


# ── sightings_refresh ─────────────────────────────────────────────────────


class TestSightingsRefresh:
    def test_refresh_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sightings/99999/refresh",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_refresh_success(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        with patch(
            "app.search_service.search_requirement",
            new=AsyncMock(return_value={"sightings": [], "source_stats": [], "mpn_results": {}}),
        ):
            resp = client.post(
                f"/v2/partials/sightings/{item.id}/refresh",
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200

    def test_refresh_search_fails(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        with patch("app.search_service.search_requirement", new=AsyncMock(side_effect=RuntimeError("search down"))):
            resp = client.post(
                f"/v2/partials/sightings/{item.id}/refresh",
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert "warning" in resp.headers["HX-Trigger"]


# ── sightings_list filters ────────────────────────────────────────────────


class TestSightingsListFilters:
    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({"status": "open"}, id="filter_by_status"),
            pytest.param({"sales_person": "Test"}, id="filter_by_sales_person"),
            pytest.param({"assigned": "mine"}, id="filter_assigned_mine"),
            pytest.param({"q": "STM32"}, id="filter_by_query"),
            pytest.param({"sort": "mpn", "dir": "asc"}, id="sort_by_mpn"),
            pytest.param({"sort": "created", "dir": "desc"}, id="sort_by_created"),
            pytest.param({"group_by": "manufacturer"}, id="group_by_manufacturer"),
            pytest.param({"group_by": "brand"}, id="group_by_brand"),
        ],
    )
    def test_list_filter(self, client: TestClient, req_with_item: tuple, params: dict):
        resp = client.get(
            "/v2/partials/sightings",
            params=params,
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_workspace_endpoint(self, client: TestClient):
        resp = client.get(
            "/v2/partials/sightings/workspace",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── sightings_detail ──────────────────────────────────────────────────────


class TestSightingsDetail:
    def test_detail_not_found(self, client: TestClient):
        resp = client.get(
            "/v2/partials/sightings/99999/detail",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_detail_found(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        resp = client.get(
            f"/v2/partials/sightings/{item.id}/detail",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "sourcing_status",
        [
            pytest.param("sourcing", id="sourcing_status_no_rfqs"),
            pytest.param("offered", id="offered_status"),
            pytest.param("quoted", id="quoted_status"),
            pytest.param("won", id="won_status"),
        ],
    )
    def test_detail_by_sourcing_status(
        self, client: TestClient, req_with_item: tuple, db_session: Session, sourcing_status: str
    ):
        _, item = req_with_item
        item.sourcing_status = sourcing_status
        db_session.commit()
        resp = client.get(
            f"/v2/partials/sightings/{item.id}/detail",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── sightings_mark_unavailable ─────────────────────────────────────────────


class TestMarkUnavailableSuccess:
    def test_mark_unavailable_success(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/mark-unavailable",
            data={"vendor_name": "Arrow Electronics", "reason": "sold_elsewhere"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── sightings_assign_buyer ─────────────────────────────────────────────────


class TestAssignBuyerWithId:
    def test_assign_buyer_with_valid_id(self, client: TestClient, req_with_item: tuple, test_user: User):
        _, item = req_with_item
        resp = client.patch(
            f"/v2/partials/sightings/{item.id}/assign",
            data={"assigned_buyer_id": str(test_user.id)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── sightings_log_activity ─────────────────────────────────────────────────


class TestLogActivity:
    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"notes": "test note", "channel": "note"}, id="note"),
            pytest.param({"notes": "called vendor", "channel": "call", "vendor_name": "Acme Vendor"}, id="call"),
            pytest.param({"notes": "sent email", "channel": "email"}, id="email"),
        ],
    )
    def test_log_activity_success(self, client: TestClient, req_with_item: tuple, data: dict):
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/log-activity",
            data=data,
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"notes": "   ", "channel": "note"}, id="empty_notes"),
            pytest.param({"notes": "hello", "channel": "invalid"}, id="invalid_channel"),
        ],
    )
    def test_log_activity_bad_request(self, client: TestClient, req_with_item: tuple, data: dict):
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/log-activity",
            data=data,
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_log_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sightings/99999/log-activity",
            data={"notes": "note", "channel": "note"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404


# ── sightings_preview_inquiry ─────────────────────────────────────────────


class TestPreviewInquiry:
    def test_preview_missing_params(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_preview_success(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        with patch("app.email_service._build_html_body", return_value="<p>Hello</p>"):
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": str(item.id),
                    "vendor_names": "Arrow Electronics",
                    "email_body": "Please quote LM317T",
                },
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200

    def test_preview_with_vendor_card(
        self, client: TestClient, req_with_item: tuple, test_vendor_card, test_vendor_contact, db_session: Session
    ):
        _, item = req_with_item
        with patch("app.email_service._build_html_body", return_value="<p>Hello</p>"):
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": str(item.id),
                    "vendor_names": test_vendor_card.display_name,
                    "email_body": "Please quote STM32",
                },
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200


# ── sightings_send_inquiry ────────────────────────────────────────────────


class TestSendInquiry:
    def test_send_missing_params(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_send_missing_email_body(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={
                "requirement_ids": str(item.id),
                "vendor_names": "Arrow Electronics",
                "email_body": "",
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_send_rfq_success(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=[{"vendor": "Arrow"}])):
            with patch("app.services.sourcing_auto_progress.auto_progress_status", return_value=False):
                resp = client.post(
                    "/v2/partials/sightings/send-inquiry",
                    data={
                        "requirement_ids": str(item.id),
                        "vendor_names": "Arrow Electronics",
                        "email_body": "Please quote STM32F4",
                    },
                    headers={"HX-Request": "true"},
                )
        assert resp.status_code == 200

    def test_send_rfq_auto_progress(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        with patch(
            "app.email_service.send_batch_rfq",
            new=AsyncMock(return_value=[{"vendor_name": "Arrow Electronics", "status": "sent"}]),
        ):
            with patch("app.services.sourcing_auto_progress.auto_progress_status", return_value=True):
                resp = client.post(
                    "/v2/partials/sightings/send-inquiry",
                    data={
                        "requirement_ids": str(item.id),
                        "vendor_names": "Arrow Electronics",
                        "email_body": "Quote request",
                    },
                    headers={"HX-Request": "true"},
                )
        assert resp.status_code == 200
        assert "advanced to sourcing" in resp.text or "sourcing" in resp.text.lower()

    def test_send_rfq_email_service_fails(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        with patch("app.email_service.send_batch_rfq", new=AsyncMock(side_effect=RuntimeError("SMTP down"))):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(item.id),
                    "vendor_names": "Arrow Electronics",
                    "email_body": "Quote request",
                },
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "Failed" in resp.text or "failed" in resp.text or "warning" in resp.text.lower()


# ── batch-refresh: skipped / failed paths ────────────────────────────────


class TestBatchRefreshAdditional:
    def test_batch_refresh_failed_path(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        with patch(
            "app.search_service.search_requirement",
            new=AsyncMock(side_effect=RuntimeError("fail")),
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item.id])},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "failed" in resp.text.lower()

    def test_batch_refresh_invalid_json(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "not-valid-json!!!{"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400


# ── vendor modal with requirements ────────────────────────────────────────


class TestVendorModalWithRequirements:
    def test_vendor_modal_with_req_ids(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        resp = client.get(
            f"/v2/partials/sightings/vendor-modal?requirement_ids={item.id}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

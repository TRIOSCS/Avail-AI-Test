"""tests/test_sightings_router_coverage2.py — Additional coverage for
app/routers/sightings.py.

Targets uncovered branches:
- _within_rate_limit: timezone-naive branch
- sightings_refresh: rate limit hit, refresh failed paths
- sightings_list: filter branches (status, sales_person, assigned, q, group_by)
- sightings_detail: not found, various suggestion paths
- sightings_mark_unavailable: success path
- sightings_assign_buyer: with buyer id
- sightings_log_activity: all channels, invalid channel, empty notes
- sightings_preview_inquiry: success, missing params
- sightings_send_inquiry: success, missing params, failed send
- batch-refresh: skipped (rate-limited) path, failed path

Called by: pytest
Depends on: conftest.py fixtures
"""

import os

os.environ["TESTING"] = "1"

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User
from app.routers.sightings import _within_rate_limit

# ── Unit tests for helpers ─────────────────────────────────────────────────


class TestWithinRateLimit:
    def test_none_last_searched(self):
        now = datetime.now(timezone.utc)
        assert _within_rate_limit(None, now) is False

    def test_naive_datetime_within_limit(self):
        # Timezone-naive datetime (SQLite) within cooldown
        ts = datetime.utcnow() - timedelta(seconds=10)
        now = datetime.now(timezone.utc)
        assert _within_rate_limit(ts, now) is True

    def test_naive_datetime_outside_limit(self):
        # Timezone-naive datetime older than cooldown
        ts = datetime.utcnow() - timedelta(seconds=400)
        now = datetime.now(timezone.utc)
        assert _within_rate_limit(ts, now) is False

    def test_naive_now_within_limit(self):
        # naive `now` should also be handled
        ts = datetime.utcnow() - timedelta(seconds=10)
        now = datetime.utcnow()
        assert _within_rate_limit(ts, now) is True

    def test_tz_aware_within_limit(self):
        now = datetime.now(timezone.utc)
        ts = now - timedelta(seconds=10)
        assert _within_rate_limit(ts, now) is True

    def test_tz_aware_outside_limit(self):
        now = datetime.now(timezone.utc)
        ts = now - timedelta(seconds=400)
        assert _within_rate_limit(ts, now) is False


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def req_with_item(db_session: Session, test_user: User) -> tuple:
    req = Requisition(
        name="COV2-REQ",
        customer_name="Cov Co",
        status="active",
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

    def test_refresh_rate_limited(self, client: TestClient, req_with_item: tuple, db_session: Session):
        _, item = req_with_item
        # Set last_searched_at to 10 seconds ago — within cooldown
        item.last_searched_at = datetime.utcnow() - timedelta(seconds=10)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/refresh",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert "Already searched" in resp.headers["HX-Trigger"]

    def test_refresh_success(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        with patch("app.search_service.search_requirement", new=AsyncMock()):
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
    def test_filter_by_status(self, client: TestClient, req_with_item: tuple):
        resp = client.get(
            "/v2/partials/sightings",
            params={"status": "open"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_filter_by_sales_person(self, client: TestClient, req_with_item: tuple):
        resp = client.get(
            "/v2/partials/sightings",
            params={"sales_person": "Test"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_filter_assigned_mine(self, client: TestClient, req_with_item: tuple):
        resp = client.get(
            "/v2/partials/sightings",
            params={"assigned": "mine"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_filter_by_query(self, client: TestClient, req_with_item: tuple):
        resp = client.get(
            "/v2/partials/sightings",
            params={"q": "STM32"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_sort_by_mpn(self, client: TestClient, req_with_item: tuple):
        resp = client.get(
            "/v2/partials/sightings",
            params={"sort": "mpn", "dir": "asc"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_sort_by_created(self, client: TestClient, req_with_item: tuple):
        resp = client.get(
            "/v2/partials/sightings",
            params={"sort": "created", "dir": "desc"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_group_by_manufacturer(self, client: TestClient, req_with_item: tuple):
        resp = client.get(
            "/v2/partials/sightings",
            params={"group_by": "manufacturer"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_group_by_brand(self, client: TestClient, req_with_item: tuple):
        resp = client.get(
            "/v2/partials/sightings",
            params={"group_by": "brand"},
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

    def test_detail_sourcing_status_no_rfqs(self, client: TestClient, req_with_item: tuple, db_session: Session):
        _, item = req_with_item
        item.sourcing_status = "sourcing"
        db_session.commit()
        resp = client.get(
            f"/v2/partials/sightings/{item.id}/detail",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_detail_offered_status(self, client: TestClient, req_with_item: tuple, db_session: Session):
        _, item = req_with_item
        item.sourcing_status = "offered"
        db_session.commit()
        resp = client.get(
            f"/v2/partials/sightings/{item.id}/detail",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_detail_quoted_status(self, client: TestClient, req_with_item: tuple, db_session: Session):
        _, item = req_with_item
        item.sourcing_status = "quoted"
        db_session.commit()
        resp = client.get(
            f"/v2/partials/sightings/{item.id}/detail",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_detail_won_status(self, client: TestClient, req_with_item: tuple, db_session: Session):
        _, item = req_with_item
        item.sourcing_status = "won"
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
            data={"vendor_name": "Arrow Electronics"},
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
    def test_log_note_success(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/log-activity",
            data={"notes": "test note", "channel": "note"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_log_call_success(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/log-activity",
            data={"notes": "called vendor", "channel": "call", "vendor_name": "Acme Vendor"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_log_email_success(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/log-activity",
            data={"notes": "sent email", "channel": "email"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_log_empty_notes(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/log-activity",
            data={"notes": "   ", "channel": "note"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_log_invalid_channel(self, client: TestClient, req_with_item: tuple):
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/log-activity",
            data={"notes": "hello", "channel": "invalid"},
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
        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=[{"vendor": "Arrow"}])):
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
    def test_batch_refresh_rate_limited_skips(self, client: TestClient, req_with_item: tuple, db_session: Session):
        _, item = req_with_item
        item.last_searched_at = datetime.utcnow() - timedelta(seconds=10)
        db_session.commit()
        with patch("app.search_service.search_requirement", new=AsyncMock()):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item.id])},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower() or "already fresh" in resp.text.lower()

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

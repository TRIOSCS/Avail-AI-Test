"""test_sightings_async_coverage.py — Tests for async HTMX sightings routes.

Uses httpx.AsyncClient with ASGITransport to properly track async route coverage.
Targets lines 687-992, 1132-1293 in app/routers/sightings.py.

Called by: pytest
Depends on: app/routers/sightings.py, tests/conftest.py
"""

import json
import os

os.environ["TESTING"] = "1"

from datetime import UTC
from unittest.mock import AsyncMock, patch

from app.models import Requirement


def _make_requirement(db_session, requisition, **overrides) -> Requirement:
    """Persist a Requirement under the given requisition; overrides win over
    defaults."""
    fields = {"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": 10}
    fields.update(overrides)
    r = Requirement(requisition_id=requisition.id, **fields)
    db_session.add(r)
    db_session.commit()
    return r


# ── batch-assign ──────────────────────────────────────────────────────


class TestBatchAssignAsync:
    async def test_batch_assign_empty_ids(self, client, db_session, test_user):
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": "[]", "buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        assert "No requirements" in resp.text

    async def test_batch_assign_valid(self, client, db_session, test_user, test_requisition):
        r = _make_requirement(db_session, test_requisition)

        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={
                "requirement_ids": json.dumps([r.id]),
                "buyer_id": str(test_user.id),
            },
        )
        assert resp.status_code == 200

    async def test_batch_assign_no_buyer(self, client, db_session, test_user, test_requisition):
        r = _make_requirement(db_session, test_requisition, primary_mpn="BC547", target_qty=5)

        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={
                "requirement_ids": json.dumps([r.id]),
                "buyer_id": "",
            },
        )
        assert resp.status_code == 200


# ── batch-status ──────────────────────────────────────────────────────


class TestBatchStatusAsync:
    async def test_batch_status_empty_ids(self, client):
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": "[]", "status": "sourcing"},
        )
        assert resp.status_code == 200
        assert "No requirements" in resp.headers.get("HX-Trigger", "")

    async def test_batch_status_invalid_status(self, client, db_session, test_user, test_requisition):
        r = _make_requirement(db_session, test_requisition)

        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={
                "requirement_ids": json.dumps([r.id]),
                "status": "not_a_valid_status",
            },
        )
        assert resp.status_code == 400

    async def test_batch_status_valid_transition(self, client, db_session, test_user, test_requisition):
        r = _make_requirement(db_session, test_requisition)

        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={
                "requirement_ids": json.dumps([r.id]),
                "status": "sourcing",
            },
        )
        assert resp.status_code == 200


# ── batch-refresh ─────────────────────────────────────────────────────


class TestBatchRefreshAsync:
    async def test_batch_refresh_empty(self, client):
        with patch("app.search_service.search_requirement", new_callable=AsyncMock) as mock_search:
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": "[]"},
            )
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "no requirements to search" in trigger.lower()

    async def test_batch_refresh_invalid_format(self, client):
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "not-valid-json"},
        )
        assert resp.status_code == 400

    async def test_batch_refresh_nonexistent_requirement(self, client):
        with patch("app.search_service.search_requirement", new_callable=AsyncMock):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([999999])},
            )
        assert resp.status_code == 200

    async def test_batch_refresh_search_exception(self, client, db_session, test_user, test_requisition):
        from datetime import datetime, timedelta

        r = _make_requirement(
            db_session,
            test_requisition,
            last_searched_at=datetime.now(UTC) - timedelta(hours=2),  # Stale
        )

        async def _fail(*args, **kwargs):
            raise Exception("Search failed")

        with patch("app.search_service.search_requirement", side_effect=_fail):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r.id])},
            )
        assert resp.status_code == 200
        # Search now runs in the background, so the immediate toast just acknowledges it.
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Searching" in trigger


# ── preview-inquiry ───────────────────────────────────────────────────


class TestPreviewInquiryAsync:
    async def test_preview_inquiry_missing_fields(self, client):
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={},
        )
        assert resp.status_code == 400

    async def test_preview_inquiry_valid(self, client, db_session, test_user, test_requisition):
        r = _make_requirement(db_session, test_requisition)

        with patch("app.routers.sightings.template_response") as mock_tpl:
            from fastapi.responses import HTMLResponse

            mock_tpl.return_value = HTMLResponse("<div>preview</div>")
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Arrow Electronics",
                    "email_body": "Please quote the following parts",
                },
            )
        assert resp.status_code == 200


# ── send-inquiry ──────────────────────────────────────────────────────


class TestSendInquiryAsync:
    async def test_send_inquiry_missing_fields(self, client):
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={},
        )
        assert resp.status_code == 400

    async def test_send_inquiry_success(self, client, db_session, test_user, test_requisition):
        r = _make_requirement(db_session, test_requisition)

        with patch("app.email_service.send_batch_rfq", new_callable=AsyncMock, return_value=[{"vendor": "Arrow"}]):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Arrow Electronics",
                    "email_body": "Please quote: LM317T",
                },
            )
        assert resp.status_code == 200

    async def test_send_inquiry_send_fails(self, client, db_session, test_user, test_requisition):
        r = _make_requirement(db_session, test_requisition)

        async def _fail_send(*args, **kwargs):
            raise Exception("Graph API error")

        with patch("app.email_service.send_batch_rfq", side_effect=_fail_send):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Arrow Electronics",
                    "email_body": "Please quote: LM317T",
                },
            )
        assert resp.status_code == 200
        assert "Failed" in resp.text or "failed" in resp.text

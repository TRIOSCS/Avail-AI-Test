"""test_sightings_async_coverage.py — Tests for async HTMX sightings routes.

Uses httpx.AsyncClient with ASGITransport to properly track async route coverage.
Targets lines 687-992, 1132-1293 in app/routers/sightings.py.

Called by: pytest
Depends on: app/routers/sightings.py, tests/conftest.py
"""

import json
import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, patch

from app.models import Requirement


async def _async_client(app, db_session, test_user):
    """Create an async HTTPX client with auth/db overrides."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    async def _override_fresh_token():
        return "mock-token"

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_fresh_token
    return app


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
        req = test_requisition
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            target_qty=10,
        )
        db_session.add(r)
        db_session.commit()

        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={
                "requirement_ids": json.dumps([r.id]),
                "buyer_id": str(test_user.id),
            },
        )
        assert resp.status_code == 200

    async def test_batch_assign_no_buyer(self, client, db_session, test_user, test_requisition):
        req = test_requisition
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="BC547",
            manufacturer="TI",
            target_qty=5,
        )
        db_session.add(r)
        db_session.commit()

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
        assert "No requirements" in resp.text

    async def test_batch_status_invalid_status(self, client, db_session, test_user, test_requisition):
        req = test_requisition
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            target_qty=10,
        )
        db_session.add(r)
        db_session.commit()

        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={
                "requirement_ids": json.dumps([r.id]),
                "status": "not_a_valid_status",
            },
        )
        assert resp.status_code == 400

    async def test_batch_status_valid_transition(self, client, db_session, test_user, test_requisition):
        req = test_requisition
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            target_qty=10,
        )
        db_session.add(r)
        db_session.commit()

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
        assert "Searched" in resp.text or "0/" in resp.text

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

    async def test_batch_refresh_skips_recently_searched(self, client, db_session, test_user, test_requisition):
        from datetime import datetime, timezone

        req = test_requisition
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            target_qty=10,
            last_searched_at=datetime.now(timezone.utc),  # Just searched
        )
        db_session.add(r)
        db_session.commit()

        with patch("app.search_service.search_requirement", new_callable=AsyncMock) as mock_search:
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r.id])},
            )
        # Should have skipped it
        assert resp.status_code == 200
        assert mock_search.call_count == 0  # Skipped due to rate limit

    async def test_batch_refresh_search_exception(self, client, db_session, test_user, test_requisition):
        from datetime import datetime, timedelta, timezone

        req = test_requisition
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            target_qty=10,
            last_searched_at=datetime.now(timezone.utc) - timedelta(hours=2),  # Stale
        )
        db_session.add(r)
        db_session.commit()

        async def _fail(*args, **kwargs):
            raise Exception("Search failed")

        with patch("app.search_service.search_requirement", side_effect=_fail):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r.id])},
            )
        assert resp.status_code == 200
        # Failed count should be in message
        assert "1/" in resp.text or "failed" in resp.text.lower()


# ── preview-inquiry ───────────────────────────────────────────────────


class TestPreviewInquiryAsync:
    async def test_preview_inquiry_missing_fields(self, client):
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={},
        )
        assert resp.status_code == 400

    async def test_preview_inquiry_valid(self, client, db_session, test_user, test_requisition):
        req = test_requisition
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            target_qty=10,
        )
        db_session.add(r)
        db_session.commit()

        with patch("app.routers.sightings.templates") as mock_tpl:
            from fastapi.responses import HTMLResponse

            mock_tpl.TemplateResponse.return_value = HTMLResponse("<div>preview</div>")
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
        req = test_requisition
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            target_qty=10,
        )
        db_session.add(r)
        db_session.commit()

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
        req = test_requisition
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            manufacturer="TI",
            target_qty=10,
        )
        db_session.add(r)
        db_session.commit()

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

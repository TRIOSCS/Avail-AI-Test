"""test_routers_sightings.py — Tests for sightings refresh structural fix.

Covers the click-to-refresh structural fix:
- source="sse" suppresses rate-guard toast and broker.publish
- source="user" (default) emits toast and publishes broker event
- X-Rendered-Req-Id header echoed on detail and refresh responses

Called by: pytest
Depends on: app/routers/sightings.py, conftest.py fixtures
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User


@pytest.fixture()
def req_with_item(db_session: Session, test_user: User) -> tuple:
    """Fresh requisition + requirement for refresh tests."""
    req = Requisition(
        name="STRUCT-FIX-REQ",
        customer_name="Struct Fix Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM741CN",
        target_qty=500,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


class TestSightingsRefreshSourceParam:
    """Source=sse vs source=user (default) behavior on rate-guard path."""

    def test_sightings_refresh_sse_source_suppresses_toast(
        self, client: TestClient, req_with_item: tuple, db_session: Session
    ):
        """Within cooldown, POST ?source=sse → 200, no HX-Trigger header."""
        _, item = req_with_item
        # Force rate-guard path: searched 10 seconds ago
        item.last_searched_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/sightings/{item.id}/refresh?source=sse",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "HX-Trigger" not in resp.headers

    def test_sightings_refresh_user_source_emits_toast(
        self, client: TestClient, req_with_item: tuple, db_session: Session
    ):
        """Within cooldown, POST without source → 200, HX-Trigger contains showToast."""
        _, item = req_with_item
        item.last_searched_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/sightings/{item.id}/refresh",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert "showToast" in resp.headers["HX-Trigger"]

    def test_sightings_refresh_sse_skips_broker_publish(self, client: TestClient, req_with_item: tuple):
        """Outside cooldown, POST ?source=sse → broker.publish NOT called."""
        _, item = req_with_item
        # No last_searched_at → outside cooldown
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            with patch("app.search_service.search_requirement", new=AsyncMock()):
                resp = client.post(
                    f"/v2/partials/sightings/{item.id}/refresh?source=sse",
                    headers={"HX-Request": "true"},
                )
            assert resp.status_code == 200
            mock_broker.publish.assert_not_called()

    def test_sightings_refresh_user_calls_broker_publish(self, client: TestClient, req_with_item: tuple):
        """Outside cooldown, POST without source → broker.publish called once with
        sighting-updated."""
        _, item = req_with_item
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            with patch("app.search_service.search_requirement", new=AsyncMock()):
                resp = client.post(
                    f"/v2/partials/sightings/{item.id}/refresh",
                    headers={"HX-Request": "true"},
                )
            assert resp.status_code == 200
            mock_broker.publish.assert_called_once()
            # Second positional arg is the event name
            args, _ = mock_broker.publish.call_args
            assert args[1] == "sighting-updated"


class TestSightingsRenderedReqIdHeader:
    """X-Rendered-Req-Id is echoed by detail and refresh endpoints."""

    def test_sightings_refresh_echoes_req_id_header(
        self, client: TestClient, req_with_item: tuple, db_session: Session
    ):
        """Both rate-guard and normal paths echo X-Rendered-Req-Id matching
        str(req_id)."""
        _, item = req_with_item

        # Rate-guard path
        item.last_searched_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
        db_session.commit()
        resp_guard = client.post(
            f"/v2/partials/sightings/{item.id}/refresh",
            headers={"HX-Request": "true"},
        )
        assert resp_guard.status_code == 200
        assert resp_guard.headers.get("X-Rendered-Req-Id") == str(item.id)

        # Normal path
        item.last_searched_at = None
        db_session.commit()
        with patch("app.search_service.search_requirement", new=AsyncMock()):
            resp_normal = client.post(
                f"/v2/partials/sightings/{item.id}/refresh",
                headers={"HX-Request": "true"},
            )
        assert resp_normal.status_code == 200
        assert resp_normal.headers.get("X-Rendered-Req-Id") == str(item.id)

    def test_sightings_detail_echoes_req_id_header(self, client: TestClient, req_with_item: tuple):
        """GET /detail response carries X-Rendered-Req-Id matching str(req_id)."""
        _, item = req_with_item
        resp = client.get(
            f"/v2/partials/sightings/{item.id}/detail",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("X-Rendered-Req-Id") == str(item.id)

    def test_sightings_refresh_within_cooldown_echoes_req_id_header(
        self, client: TestClient, req_with_item: tuple, db_session: Session
    ):
        """Rate-limited (cooldown) path explicitly returns X-Rendered-Req-Id.

        Pins the contract that the header is set even when the search is
        skipped — the detail endpoint sets it once on every response and
        sightings_refresh inherits via `await sightings_detail(...)`.
        """
        _, item = req_with_item
        item.last_searched_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/sightings/{item.id}/refresh",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("X-Rendered-Req-Id") == str(item.id)


class TestSightingsRefreshSourceValidation:
    """Source=Literal[user|sse] — FastAPI rejects unknown values with 422."""

    def test_sightings_refresh_unknown_source_rejected_with_422(self, client: TestClient, req_with_item: tuple):
        """?source=foo → 422.

        Closes the silent re-enable of toast + broker.publish loop on typos like
        ?source=SSE (any value other than user/sse used to fall into the user-path
        branch).
        """
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/refresh?source=foo",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422

    def test_sightings_refresh_uppercase_sse_rejected_with_422(self, client: TestClient, req_with_item: tuple):
        """?source=SSE → 422 (Literal is case-sensitive)."""
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/refresh?source=SSE",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422


class TestSightingsRefreshFailureToast:
    """Refresh-failure toast must surface for user clicks but never for SSE."""

    def test_sightings_refresh_sse_suppresses_failure_toast(self, client: TestClient, req_with_item: tuple):
        """search_requirement raises + ?source=sse → 200, no HX-Trigger.

        Background-fired SSE refreshes must not surface user-targeted warning toasts.
        """
        from unittest.mock import AsyncMock

        _, item = req_with_item
        boom = AsyncMock(side_effect=RuntimeError("connector down"))
        with patch("app.search_service.search_requirement", new=boom):
            with patch("app.routers.sightings.broker") as mock_broker:
                mock_broker.publish = AsyncMock()
                resp = client.post(
                    f"/v2/partials/sightings/{item.id}/refresh?source=sse",
                    headers={"HX-Request": "true"},
                )
        assert resp.status_code == 200
        assert "HX-Trigger" not in resp.headers

    def test_sightings_refresh_user_emits_failure_toast(self, client: TestClient, req_with_item: tuple):
        """search_requirement raises + no source param → 200, HX-Trigger contains
        'Search refresh failed'."""
        from unittest.mock import AsyncMock

        _, item = req_with_item
        boom = AsyncMock(side_effect=RuntimeError("connector down"))
        with patch("app.search_service.search_requirement", new=boom):
            with patch("app.routers.sightings.broker") as mock_broker:
                mock_broker.publish = AsyncMock()
                resp = client.post(
                    f"/v2/partials/sightings/{item.id}/refresh",
                    headers={"HX-Request": "true"},
                )
        assert resp.status_code == 200
        assert "HX-Trigger" in resp.headers
        assert "Search refresh failed" in resp.headers["HX-Trigger"]

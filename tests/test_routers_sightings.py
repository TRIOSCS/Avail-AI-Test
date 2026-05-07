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

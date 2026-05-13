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


class TestSightingsClickPendingCounter:
    """Static-grep regression: click-pending state is a counter, not a bool.

    The earlier `clickInFlight` boolean broke under multi-click races: clicking
    row A then row B before A returns let A's afterRequest clear the flag
    while B's POST was still in-flight, opening a window for SSE-fired
    redundant POSTs (correctness preserved by X-Rendered-Req-Id, but the
    suppression invariant was broken). Replaced with `clickPending` counter.
    These tests catch a future revert.
    """

    def test_no_click_in_flight_field_in_htmx_app_js(self):
        """htmx_app.js must not reintroduce the clickInFlight boolean."""
        from pathlib import Path

        js = Path("app/static/htmx_app.js").read_text()
        assert "clickInFlight" not in js, (
            "clickInFlight reintroduced in htmx_app.js — multi-click race regression. Use clickPending counter instead."
        )

    def test_no_click_in_flight_field_in_sightings_list_template(self):
        """sightings/list.html must not reintroduce the clickInFlight boolean."""
        from pathlib import Path

        html = Path("app/templates/htmx/partials/sightings/list.html").read_text()
        assert "clickInFlight" not in html, (
            "clickInFlight reintroduced in sightings/list.html — multi-click race regression. "
            "Use clickPending counter instead."
        )

    def test_click_pending_counter_present_in_htmx_app_js(self):
        """htmx_app.js exposes the clickPending counter on the sightingSelection
        store."""
        from pathlib import Path

        js = Path("app/static/htmx_app.js").read_text()
        assert "clickPending: 0" in js, "clickPending counter missing from sightingSelection store"
        # Decrement uses Math.max clamp to guard against double-decrement.
        assert "Math.max(0, store.clickPending - 1)" in js, "clickPending decrement must clamp at 0 via Math.max"

    def test_click_pending_counter_present_in_sightings_list_template(self):
        """sightings/list.html increments on click and gates SSE on counter > 0.

        selectReq() fires both GET /detail (cached panel ~100ms) and POST /refresh
        (background search), so clickPending must increment by 2 — once per in-flight
        request targeting #sightings-detail. The htmx:afterRequest listener decrements
        once per response, returning the counter to 0.
        """
        from pathlib import Path

        html = Path("app/templates/htmx/partials/sightings/list.html").read_text()
        assert "store.clickPending += 2" in html, (
            "selectReq() must increment clickPending by 2 (one for /detail, one for /refresh)"
        )
        assert "store.clickPending > 0" in html, "SSE handler must gate on clickPending > 0"


class TestSightingsDetailDoesNotSearch:
    """GET /detail must NOT run the search pipeline.

    The frontend selectReq() fires GET /detail in parallel with POST /refresh so the
    cached panel paints in ~100ms while the search runs in the background. If /detail
    ever started calling search_requirement(), it would defeat the fast-feedback
    contract and double the search load on every click.
    """

    def test_sightings_detail_does_not_call_search_requirement(self, client: TestClient, req_with_item: tuple):
        """GET /detail returns 200 + rendered detail without invoking
        search_requirement."""
        _, item = req_with_item
        with patch("app.search_service.search_requirement", new=AsyncMock()) as mock_search:
            resp = client.get(
                f"/v2/partials/sightings/{item.id}/detail",
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert resp.headers.get("X-Rendered-Req-Id") == str(item.id)
        mock_search.assert_not_called()

    def test_sightings_refresh_does_call_search_requirement(self, client: TestClient, req_with_item: tuple):
        """POST /refresh DOES call search_requirement (contract counter-test to
        /detail).

        Pins the asymmetry the frontend relies on: /detail is fast cached read,
        /refresh runs the pipeline. Together they form the click-to-refresh
        pattern in selectReq().
        """
        _, item = req_with_item
        with patch("app.search_service.search_requirement", new=AsyncMock()) as mock_search:
            with patch("app.routers.sightings.broker") as mock_broker:
                mock_broker.publish = AsyncMock()
                resp = client.post(
                    f"/v2/partials/sightings/{item.id}/refresh?source=user",
                    headers={"HX-Request": "true"},
                )
        assert resp.status_code == 200
        assert resp.headers.get("X-Rendered-Req-Id") == str(item.id)
        mock_search.assert_called_once()


class TestSightingsListTemplateSelectReqShape:
    """Static-grep guard: selectReq fires both GET /detail and POST /refresh.

    The earlier single-POST shape blocked the UI for ~6s because every click
    waited on the full search pipeline. The current shape fires GET /detail
    (cached, ~100ms) concurrently with POST /refresh?source=user. These
    static-grep checks ensure neither leg is silently removed.
    """

    def test_selectreq_fires_detail_get(self):
        """SelectReq must call htmx.ajax GET /detail."""
        from pathlib import Path

        html = Path("app/templates/htmx/partials/sightings/list.html").read_text()
        assert "htmx.ajax('GET', '/v2/partials/sightings/' + id + '/detail'" in html, (
            "selectReq must fire GET /detail for fast cached paint"
        )

    def test_selectreq_fires_refresh_post_with_user_source(self):
        """SelectReq must call htmx.ajax POST /refresh?source=user."""
        from pathlib import Path

        html = Path("app/templates/htmx/partials/sightings/list.html").read_text()
        assert "htmx.ajax('POST', '/v2/partials/sightings/' + id + '/refresh?source=user'" in html, (
            "selectReq must fire POST /refresh?source=user for background search"
        )

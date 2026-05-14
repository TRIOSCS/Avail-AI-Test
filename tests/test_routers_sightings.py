"""test_routers_sightings.py — Tests for sightings refresh structural fix.

Covers the click-to-refresh structural fix:
- source="sse" suppresses broker.publish to prevent self-trigger loops
- source="user" (default) publishes broker event and emits per-MPN toast
- X-Rendered-Req-Id header echoed on detail and refresh responses
- Per-MPN cooldown (48h, MaterialCard-level) replaces the prior 5-min
  per-requirement cooldown

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
    """Source=sse vs source=user (default) behavior."""

    def test_sightings_refresh_sse_skips_broker_publish(self, client: TestClient, req_with_item: tuple):
        """POST ?source=sse → broker.publish NOT called."""
        _, item = req_with_item
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            with patch(
                "app.search_service.search_requirement",
                new=AsyncMock(return_value={"sightings": [], "source_stats": [], "mpn_results": {}}),
            ):
                resp = client.post(
                    f"/v2/partials/sightings/{item.id}/refresh?source=sse",
                    headers={"HX-Request": "true"},
                )
            assert resp.status_code == 200
            mock_broker.publish.assert_not_called()

    def test_sightings_refresh_user_calls_broker_publish(self, client: TestClient, req_with_item: tuple):
        """POST without source → broker.publish called once with sighting-updated."""
        _, item = req_with_item
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            with patch(
                "app.search_service.search_requirement",
                new=AsyncMock(return_value={"sightings": [], "source_stats": [], "mpn_results": {}}),
            ):
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
        """POST /refresh response carries X-Rendered-Req-Id matching str(req_id)."""
        _, item = req_with_item

        with patch(
            "app.search_service.search_requirement",
            new=AsyncMock(return_value={"sightings": [], "source_stats": [], "mpn_results": {}}),
        ):
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
        with patch(
            "app.search_service.search_requirement",
            new=AsyncMock(return_value={"sightings": [], "source_stats": [], "mpn_results": {}}),
        ) as mock_search:
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


class TestCrossMpnSightingVisibility:
    """Detail panel surfaces sightings linked via material_card_id from prior searches
    on other requirements that share the same primary or substitute MPN.

    Closes the cross-requirement gap: when req1 searches MPN X and a vendor
    sighting is materialized against MaterialCard(X), opening req2's detail
    panel (which also targets MPN X) must show that vendor — even though the
    sighting was created with requirement_id=req1.item.id.
    """

    def test_detail_shows_sightings_from_other_req_via_material_card(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        from app.models import MaterialCard, Requirement, Requisition
        from app.models.sourcing import Sighting
        from app.services.sighting_aggregation import rebuild_vendor_summaries
        from app.utils.normalization import normalize_mpn_key

        # Two requisitions, two requirements, but both point at the same MPN
        # via a shared MaterialCard.
        req1 = Requisition(
            name="R1",
            customer_name="C",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        req2 = Requisition(
            name="R2",
            customer_name="C",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([req1, req2])
        db_session.flush()

        card = MaterialCard(
            normalized_mpn=normalize_mpn_key("SHARED"),
            display_mpn="SHARED",
        )
        db_session.add(card)
        db_session.flush()

        item1 = Requirement(
            requisition_id=req1.id,
            primary_mpn="SHARED",
            material_card_id=card.id,
            created_at=datetime.now(timezone.utc),
        )
        item2 = Requirement(
            requisition_id=req2.id,
            primary_mpn="SHARED",
            material_card_id=card.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([item1, item2])
        db_session.flush()

        # Sighting created during req1's search — linked to material_card,
        # NOT to req2's requirement_id directly.
        s = Sighting(
            requirement_id=item1.id,
            material_card_id=card.id,
            vendor_name="DigiKey",
            normalized_mpn=normalize_mpn_key("SHARED"),
            source_type="api",
            unit_price=1.0,
            qty_available=100,
            score=50.0,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()

        # Rebuild summaries so detail panel has rows to render
        rebuild_vendor_summaries(db_session, item2.id)
        db_session.commit()

        # GET /detail for item2 — should include the DigiKey vendor row
        # via shared MaterialCard linkage. vendor_name is lower-cased on
        # write into VendorSightingSummary, so the rendered cell is "digikey".
        resp = client.get(f"/v2/partials/sightings/{item2.id}/detail")
        assert resp.status_code == 200
        assert "digikey" in resp.text.lower()


class TestRefreshPerMpnToast:
    """Per-MPN toast on /refresh describes how many MPNs were searched vs cached.

    Replaces the prior 5-minute per-requirement cooldown toast. The 48h
    per-MPN cooldown lives in `search_requirement` via
    MaterialCard.last_searched_at; this endpoint just surfaces the result.
    """

    def test_toast_describes_searched_and_cached_mpns(self, client: TestClient, db_session: Session, test_user: User):
        from app.models import MaterialCard, Requirement, Requisition
        from app.utils.normalization import normalize_mpn_key

        now = datetime.now(timezone.utc)
        r = Requisition(
            name="R",
            customer_name="C",
            status="active",
            created_by=test_user.id,
            created_at=now,
        )
        db_session.add(r)
        db_session.flush()
        item = Requirement(
            requisition_id=r.id,
            primary_mpn="ALPHA",
            substitutes=[{"mpn": "BETA"}],
            created_at=now,
        )
        db_session.add(item)
        # ALPHA cached (12h ago), BETA stale (no card → searched)
        db_session.add(
            MaterialCard(
                display_mpn="ALPHA",
                normalized_mpn=normalize_mpn_key("ALPHA"),
                last_searched_at=now - timedelta(hours=12),
            )
        )
        db_session.commit()

        with (
            patch("app.search_service._fetch_fresh", new=AsyncMock(return_value=([], []))),
            patch("app.services.ics_worker.queue_manager.enqueue_for_ics_search"),
            patch("app.services.nc_worker.queue_manager.enqueue_for_nc_search"),
        ):
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")

        assert resp.status_code == 200
        # HX-Trigger header carries a showToast with both counts
        hx = resp.headers.get("HX-Trigger", "")
        assert '"showToast"' in hx
        assert "1 cached" in hx
        assert ("1 search" in hx) or ("Searched 1" in hx)

    def test_all_cached_returns_no_search_toast(self, client: TestClient, db_session: Session, test_user: User):
        from app.models import MaterialCard, Requirement, Requisition
        from app.utils.normalization import normalize_mpn_key

        now = datetime.now(timezone.utc)
        r = Requisition(
            name="R",
            customer_name="C",
            status="active",
            created_by=test_user.id,
            created_at=now,
        )
        db_session.add(r)
        db_session.flush()
        item = Requirement(
            requisition_id=r.id,
            primary_mpn="ONLY",
            created_at=now,
        )
        db_session.add(item)
        db_session.add(
            MaterialCard(
                display_mpn="ONLY",
                normalized_mpn=normalize_mpn_key("ONLY"),
                last_searched_at=now - timedelta(hours=1),
            )
        )
        db_session.commit()

        with patch("app.search_service._fetch_fresh", new=AsyncMock(return_value=([], []))) as fetch_mock:
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")

        assert resp.status_code == 200
        # _fetch_fresh NOT called because all MPNs cached
        fetch_mock.assert_not_called()
        hx = resp.headers.get("HX-Trigger", "")
        assert ("All MPNs" in hx) or ("cached" in hx)

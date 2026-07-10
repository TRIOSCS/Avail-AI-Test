"""test_routers_sightings.py — Tests for sightings refresh structural fix.

Covers the click-to-refresh structural fix:
- source="sse" is the SSE render path: no search scheduled, no broker.publish
- source="user" (default) schedules the search as a background job and returns the
  immediate "Searching…" panel; the background job publishes the sighting-updated SSE
- X-Rendered-Req-Id header echoed on detail and refresh responses
- Per-MPN cooldown (48h, MaterialCard-level) still enforced inside search_requirement

Called by: pytest
Depends on: app/routers/sightings.py, conftest.py fixtures
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM741CN",
        target_qty=500,
        sourcing_status="open",
        created_at=datetime.now(UTC),
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
    """Source=Literal[user|sse] — FastAPI rejects unknown values with 422.

    foo: arbitrary typo used to fall into the user-path branch, silently
    re-enabling the toast + broker.publish loop.
    SSE: Literal is case-sensitive, so the uppercase variant is also rejected.
    """

    @pytest.mark.parametrize("source", ["foo", "SSE"])
    def test_sightings_refresh_unknown_source_rejected_with_422(
        self, client: TestClient, req_with_item: tuple, source: str
    ):
        """?source=<unknown> → 422."""
        _, item = req_with_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/refresh?source={source}",
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 422


class TestSightingsRefreshFailureToast:
    """The search now runs in the background, so a connector failure never surfaces a
    synchronous toast on the immediate refresh response — for user clicks OR for SSE."""

    def test_sightings_refresh_sse_suppresses_failure_toast(self, client: TestClient, req_with_item: tuple):
        """?source=sse → 200, no failure toast (the SSE render path never searches)."""
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

    def test_sightings_refresh_user_returns_searching_without_failure_toast(
        self, client: TestClient, req_with_item: tuple
    ):
        """A user click returns the immediate "Searching…" panel; because the search is
        scheduled in the background, a connector failure can no longer emit a
        synchronous 'Search refresh failed' toast."""
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
        assert "Searching suppliers" in resp.text
        assert "Search refresh failed" not in resp.headers.get("HX-Trigger", "")


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
        js = Path("app/static/htmx_app.js").read_text()
        assert "clickInFlight" not in js, (
            "clickInFlight reintroduced in htmx_app.js — multi-click race regression. Use clickPending counter instead."
        )

    def test_no_click_in_flight_field_in_sightings_list_template(self):
        """sightings/list.html must not reintroduce the clickInFlight boolean."""
        html = Path("app/templates/htmx/partials/sightings/list.html").read_text()
        assert "clickInFlight" not in html, (
            "clickInFlight reintroduced in sightings/list.html — multi-click race regression. "
            "Use clickPending counter instead."
        )

    def test_click_pending_counter_present_in_htmx_app_js(self):
        """htmx_app.js exposes the clickPending counter on the sightingSelection
        store."""
        js = Path("app/static/htmx_app.js").read_text()
        assert "clickPending: 0" in js, "clickPending counter missing from sightingSelection store"
        # Decrement uses Math.max clamp to guard against double-decrement.
        assert "Math.max(0, store.clickPending - 1)" in js, "clickPending decrement must clamp at 0 via Math.max"

    def test_click_pending_counter_present_in_sightings_list_template(self):
        """SelectReq fires ONE request (GET /detail), so the counter increments by 1.

        SSE handler still consults it to suppress background refreshes while a user
        click is in flight.
        """
        html = Path("app/templates/htmx/partials/sightings/list.html").read_text()
        assert "store.clickPending += 1" in html, (
            "selectReq() must increment clickPending by 1 (one GET /detail; row click is read-only)"
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

    def test_sightings_refresh_schedules_background_search(
        self, client: TestClient, req_with_item: tuple, test_user: User
    ):
        """POST /refresh SCHEDULES the search as a background job (contract counter-test
        to /detail, which never searches).

        The search is not awaited inline; the immediate response is the "Searching…"
        panel, and the SSE stream swaps results in later.
        """
        _, item = req_with_item
        scheduled = MagicMock()
        real_search = AsyncMock(return_value={"sightings": [], "source_stats": [], "mpn_results": {}})
        with (
            patch("app.routers.sightings._run_search_and_publish", new=scheduled),
            patch("app.search_service.search_requirement", new=real_search),
        ):
            resp = client.post(
                f"/v2/partials/sightings/{item.id}/refresh?source=user",
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert resp.headers.get("X-Rendered-Req-Id") == str(item.id)
        scheduled.assert_called_once_with([item.id], test_user.id)
        real_search.assert_not_called()


class TestSightingsListTemplateSelectReqShape:
    """Row click on /v2/sightings is read-only: fires GET /detail only.

    The only way to trigger a connector search is the per-row refresh icon (table.html)
    or the detail panel's "Search" button (m.search_button in _macros.html). Both POST
    /refresh which is gated by the 48h per-MPN cooldown enforced in search_requirement.
    """

    def test_selectreq_fires_detail_get(self):
        """SelectReq must call htmx.ajax GET /detail."""
        html = Path("app/templates/htmx/partials/sightings/list.html").read_text()
        assert "htmx.ajax('GET', '/v2/partials/sightings/' + id + '/detail'" in html, (
            "selectReq must fire GET /detail for fast cached paint"
        )

    def test_selectreq_does_not_fire_refresh_post(self):
        """SelectReq must NOT fire POST /refresh.

        The detail panel's m.search_button and the per-row refresh icon are the only
        places that POST /refresh — selectReq must not.
        """
        html = Path("app/templates/htmx/partials/sightings/list.html").read_text()
        # Scope the check to the selectReq function body via a static slice.
        select_req_start = html.index("selectReq(id) {")
        select_req_end = html.index("closeMobileDetail()", select_req_start)
        select_req_body = html[select_req_start:select_req_end]
        assert "/refresh" not in select_req_body, "selectReq must not POST /refresh — row click is read-only"


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
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(UTC),
        )
        req2 = Requisition(
            name="R2",
            customer_name="C",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(UTC),
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
            created_at=datetime.now(UTC),
        )
        item2 = Requirement(
            requisition_id=req2.id,
            primary_mpn="SHARED",
            material_card_id=card.id,
            created_at=datetime.now(UTC),
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
            created_at=datetime.now(UTC),
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


# NOTE: the per-MPN "searched vs cached" toast (formerly TestRefreshPerMpnToast) was
# removed when /refresh became non-blocking — the search now runs in a background job, so
# the immediate response cannot know the per-MPN counts. The 48h per-MPN cooldown still
# lives in search_requirement (MaterialCard.last_searched_at); the async contract is
# covered by tests/test_sightings_refresh_async.py.


class TestPreviewInlineEmailFix:
    """S4: inline email-fix mini-form in preview + normalized_name in preview ctx.

    Covers:
    - sightings_preview_inquiry includes ``normalized_name`` in each preview dict
      so the template can key the fix-form by it.
    - After posting a contact email to composer-vendor for a skipped vendor,
      re-previewing resolves vendor_email and omits the no-email chip.
    - The inline mini-form is rendered (HTML presence) when skip_reason==no_email.
    - The fix-email JS helper (fixVendorEmail) is present in htmx_app.js and
      calls loadPreview on success.
    """

    @pytest.fixture()
    def _rfq_fixtures(self, db_session: Session, test_user: User):
        """Requisition + requirement + vendor card WITHOUT a contact."""
        from app.models import Requirement, Requisition, VendorCard

        req = Requisition(
            name="FIX-EMAIL-REQ",
            customer_name="Fix Co",
            status="open",
            created_by=test_user.id,
            created_at=datetime.now(UTC),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="FIX123",
            target_qty=10,
            sourcing_status="open",
            created_at=datetime.now(UTC),
        )
        db_session.add(item)
        card = VendorCard(
            normalized_name="acme",
            display_name="Acme Corp",
            emails=[],
            phones=[],
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(req)
        db_session.refresh(item)
        db_session.refresh(card)
        return req, item, card

    def test_preview_includes_normalized_name_in_each_entry(
        self, client: TestClient, db_session: Session, test_user: User, _rfq_fixtures
    ):
        """preview_inquiry response context includes normalized_name per vendor so the
        template inline-form can key by it."""
        _, item, card = _rfq_fixtures
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(item.id),
                "vendor_names": "Acme Corp",
                "email_body": "Please quote.",
            },
        )
        assert resp.status_code == 200
        # The no-email chip is rendered (vendor has no contact yet)
        assert "no email" in resp.text.lower()
        # The inline fix-email form must be present (input + button).
        # Assert the tojson-encoded normalized_name is embedded in the @click attribute.
        # tojson emits double-quoted strings — "acme" is the normalized form of "Acme Corp"
        # (normalize_vendor_name strips common suffixes). This verifies the normalized_name
        # field is in the fix-form attribute, distinct from display_name "Acme Corp" which
        # renders unquoted; a regression dropping normalized_name from the preview dict
        # would break the assertion because normalize_vendor_name('Acme Corp') == 'acme',
        # not 'Acme Corp', so display_name presence alone cannot satisfy it.
        assert '"acme"' in resp.text

    def test_add_email_then_repreview_resolves_vendor_email(
        self, client: TestClient, db_session: Session, test_user: User, _rfq_fixtures
    ):
        """Posting email to composer-vendor for a skipped vendor then re-previewing
        shows vendor_email populated and no longer marks the vendor as no_email."""
        _, item, card = _rfq_fixtures

        # Step 1: add contact email via composer-vendor
        add_resp = client.post(
            "/v2/partials/sightings/composer-vendor",
            data={
                "vendor_name": "Acme Corp",
                "email": "sales@acme.example.com",
                "requirement_ids": str(item.id),
            },
        )
        assert add_resp.status_code == 200

        # Step 2: re-preview — Acme Corp now has a contact so vendor_email resolves
        preview_resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(item.id),
                "vendor_names": "acme corp",  # normalized form Alpine sends
                "email_body": "Please quote.",
            },
        )
        assert preview_resp.status_code == 200
        html = preview_resp.text
        # vendor email is shown (not the no-email chip)
        assert "sales@acme.example.com" in html
        # The amber "no email" chip is gone
        assert "no email — will be skipped" not in html

    def test_preview_inline_form_rendered_for_no_email_vendor(
        self, client: TestClient, db_session: Session, test_user: User, _rfq_fixtures
    ):
        """Template renders inline mini-form (email input + Add & re-check button) when
        skip_reason == no_email, not just the amber chip."""
        _, item, card = _rfq_fixtures
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": str(item.id),
                "vendor_names": "Acme Corp",
                "email_body": "Please quote.",
            },
        )
        assert resp.status_code == 200
        html = resp.text
        # Inline form must contain an email input and submit action
        assert 'type="email"' in html
        assert "Add" in html  # "Add & re-check" button text

    def test_fix_vendor_email_js_method_present(self):
        """FixVendorEmail method exists in rfqVendorModal and calls loadPreview."""
        from pathlib import Path

        js = Path("app/static/htmx_app.js").read_text()
        assert "fixVendorEmail(" in js, "fixVendorEmail method missing from rfqVendorModal"
        # After a successful fix, loadPreview() must be called
        # Find the fixVendorEmail function body
        start = js.index("fixVendorEmail(")
        # Grab the next ~40 lines to find loadPreview() call inside
        snippet = js[start : start + 1500]
        assert "loadPreview()" in snippet, "fixVendorEmail must call loadPreview() on success"

    def test_failed_composer_vendor_post_does_not_silently_succeed(
        self, client: TestClient, db_session: Session, test_user: User, _rfq_fixtures
    ):
        """A bad email (no @) POSTed to composer-vendor returns 4xx, not 200.

        Ensures the JS can detect failure and keep the form open (not silent success).
        """
        _, item, _ = _rfq_fixtures
        resp = client.post(
            "/v2/partials/sightings/composer-vendor",
            data={
                "vendor_name": "Acme Corp",
                "email": "not-an-email",
                "requirement_ids": str(item.id),
            },
        )
        # The endpoint returns 400 for invalid email — JS can detect !resp.ok
        assert resp.status_code == 400


class TestPerRowSearchIconAlwaysVisible:
    """The per-row refresh icon must render on every row regardless of last_searched_at
    (no 'stale' conditional).

    Its hx-post target is the same /refresh endpoint the detail-panel button uses.
    """

    def test_row_refresh_icon_has_no_stale_only_conditional(self):
        path = Path(__file__).parent.parent / "app" / "templates" / "htmx" / "partials" / "sightings" / "table.html"
        text = path.read_text()
        # The icon block must NOT be wrapped in a {% if ... is_stale %} or
        # similar Jinja conditional. Locate the hx-post for /refresh and
        # walk backwards to assert there's no stale conditional pattern
        # within ~10 lines above.
        idx = text.index('hx-post="/v2/partials/sightings/{{ r.id }}/refresh"')
        prefix = text[:idx]
        recent = "\n".join(prefix.splitlines()[-10:])
        assert "is_stale" not in recent
        assert "stale_warning" not in recent
        # The "row is stale" gate looks like {% if r.is_stale %} or
        # {% if not r.last_searched_at %} or similar. Detect direct gating
        # on row staleness — but allow unrelated Jinja conditionals
        # (e.g. {% if cards_map.get(...) %}) which are fine.
        bad_gates = [
            "{% if r.is_stale",
            "{% if not r.last_searched_at",
            "{% if r.last_searched_at < ",
        ]
        for gate in bad_gates:
            assert gate not in recent, f"Found stale-only gate: {gate!r}"

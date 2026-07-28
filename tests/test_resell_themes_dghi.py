"""test_resell_themes_dghi.py — Tests for the resell deep-review themes D/G/H/I batch.

Covers: #13 open-lens stage-token mapping + neutral badge, #15 deep-link draft Delete
(HX-Redirect), #16 single server-side import toast, #17 rows-only search swap, #18
visible-offer chip count, #20 tz-offset close_at parsing, #50 can_offer posted gate,
#51 empty-state branch order, #6 nudge headroom, #35/49 buyer-panel line scoping,
#31 import-confirm shape guard, #33/42 excess_service.add_line, #34 preview draft-only
409, #40 site-id preservation, #41 confirm_import owned-draft guard, #48 foreign-draft
404-mask sweep, the B3 degraded-row Log actions, and the resell perf items (left-list
cap/load-more, offer-compare direct query, batch follow-up tasks).

Called by: pytest
Depends on: tests.conftest (db_session, client, test_company), app.routers.resell,
            app.services.excess_service / buyer_affinity_service / task_service.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import (
    ExcessListStatus,
    ExcessOfferScope,
    ExcessOfferStatus,
    ExcessOutreachChannel,
    ExcessOutreachStatus,
)
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine, ExcessOutreach
from app.services import excess_service
from app.utils.normalization import normalize_mpn_key


@pytest.fixture()
def owner(db_session: Session) -> User:
    u = User(email="dghi-owner@trioscs.com", name="DGHI Owner", role="trader", azure_id="dghi-owner-1")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def broker(db_session: Session) -> User:
    u = User(email="dghi-broker@trioscs.com", name="DGHI Broker", role="buyer", azure_id="dghi-broker-1")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _make_list(
    db: Session,
    owner: User,
    company: Company,
    *,
    status: str = ExcessListStatus.DRAFT,
    title: str = "DGHI list",
    lines: int = 1,
) -> ExcessList:
    el = ExcessList(
        title=title,
        company_id=company.id,
        owner_id=owner.id,
        status=status,
        total_line_items=lines,
    )
    db.add(el)
    db.flush()
    for i in range(lines):
        pn = f"DGHI-PART-{i}"
        db.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number=pn,
                normalized_part_number=normalize_mpn_key(pn),
                quantity=10,
                condition="New",
            )
        )
    db.commit()
    db.refresh(el)
    return el


def _as_user(client, user):
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user

    def restore():
        app.dependency_overrides.pop(require_user, None)

    return restore


# ── #13: open-lens stage-token mapping + neutral badge ───────────────


class TestOpenLensCollectingOracle:
    def test_open_lens_stage_collecting_equals_stage_open(self, client, db_session, owner, broker, test_company):
        """A non-owner diffing stage=collecting against stage=open must learn nothing:

        both map to the live set.
        """
        el_open = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN, title="Open one")
        el_coll = _make_list(db_session, owner, test_company, status=ExcessListStatus.COLLECTING, title="Coll one")
        restore = _as_user(client, broker)
        try:
            by_open = client.get("/v2/partials/resell/lists?lens=open&stage=open").text
            by_coll = client.get("/v2/partials/resell/lists?lens=open&stage=collecting").text
            by_live = client.get("/v2/partials/resell/lists?lens=open&stage=live").text
        finally:
            restore()
        for body in (by_open, by_coll, by_live):
            assert f"Excess listing #{el_open.id}" in body
            assert f"Excess listing #{el_coll.id}" in body

    def test_open_lens_clicked_pill_keeps_active_state(self, client, db_session, owner, broker, test_company):
        """The non-owner merge must hit only the FILTER, not the echoed token: the pill
        the broker clicked keeps its active styling (a pure echo of the request — no
        server state — so no oracle), while the rows are still the merged live set."""
        _make_list(db_session, owner, test_company, status=ExcessListStatus.COLLECTING, title="Coll pill")
        restore = _as_user(client, broker)
        try:
            body = client.get("/v2/partials/resell/lists?lens=open&stage=collecting").text
        finally:
            restore()
        active = [chunk for chunk in body.split("<button") if "bg-accent-500" in chunk]
        assert len(active) == 1, "exactly one filter pill must carry the active styling"
        assert "stage=collecting" in active[0], "the active pill must be the one the broker clicked"

    def test_owner_mine_lens_stage_collecting_stays_strict(self, client, db_session, owner, test_company):
        el_open = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN, title="Mine open")
        el_coll = _make_list(db_session, owner, test_company, status=ExcessListStatus.COLLECTING, title="Mine coll")
        restore = _as_user(client, owner)
        try:
            body = client.get("/v2/partials/resell/lists?lens=mine&stage=collecting").text
        finally:
            restore()
        assert "Mine coll" in body
        assert "Mine open" not in body
        assert el_open is not None and el_coll is not None

    def test_open_lens_card_never_renders_collecting_badge(self, client, db_session, owner, broker, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.COLLECTING, title="Coll badge")
        restore = _as_user(client, broker)
        try:
            body = client.get("/v2/partials/resell/list-rows?lens=open").text
        finally:
            restore()
        assert f"Excess listing #{el.id}" in body  # the card renders…
        assert "collecting" not in body.lower()  # …but never the collecting badge

    def test_non_owner_detail_header_never_renders_collecting_badge(
        self, client, db_session, owner, broker, test_company
    ):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.COLLECTING, title="Coll hdr")
        restore = _as_user(client, broker)
        try:
            body = client.get(f"/v2/partials/resell/{el.id}").text
        finally:
            restore()
        assert "collecting" not in body.lower()

    def test_owner_still_sees_collecting_badge(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.COLLECTING, title="Own coll")
        restore = _as_user(client, owner)
        try:
            body = client.get(f"/v2/partials/resell/{el.id}").text
        finally:
            restore()
        assert "collecting" in body.lower()


# ── #15: deep-link draft Delete — route answers HX-Redirect ─────────


class TestDeleteListRedirect:
    def test_delete_answers_hx_redirect_to_workspace(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company)
        restore = _as_user(client, owner)
        try:
            resp = client.delete(f"/api/resell/{el.id}")
        finally:
            restore()
        assert resp.status_code == 200
        assert resp.headers.get("HX-Redirect") == "/v2/resell"
        assert "showToast" in resp.headers.get("HX-Trigger", "")
        assert db_session.get(ExcessList, el.id) is None

    def test_delete_button_targets_portable_selector(self, client, db_session, owner, test_company):
        """The Delete button must not target #resell-list-body (missing on a deep-linked
        full page) — it targets the detail root, which exists in both contexts."""
        el = _make_list(db_session, owner, test_company)
        restore = _as_user(client, owner)
        try:
            body = client.get(f"/v2/partials/resell/{el.id}").text
        finally:
            restore()
        assert f'hx-delete="/api/resell/{el.id}"' in body
        delete_chunk = body.split(f'hx-delete="/api/resell/{el.id}"')[1][:300]
        assert "#resell-list-body" not in delete_chunk
        assert "data-resell-detail-root" in delete_chunk


# ── #16: single server-side import toast ─────────────────────────────


class TestImportConfirmToast:
    def test_success_with_skips_emits_one_combined_toast(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, lines=0)
        rows = [
            {"part_number": "LM358N", "quantity": 5},
            {"part_number": "", "quantity": 5},  # skipped: blank part number
        ]
        restore = _as_user(client, owner)
        try:
            resp = client.post(f"/api/resell/{el.id}/import-confirm", data={"rows_json": json.dumps(rows)})
        finally:
            restore()
        assert resp.status_code == 200
        trigger = json.loads(resp.headers["HX-Trigger"])
        msg = trigger["showToast"]["message"]
        assert "Imported 1 line" in msg
        assert "1 row(s) skipped" in msg
        assert trigger["showToast"]["type"] == "warning"

    def test_clean_success_emits_success_toast(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, lines=0)
        rows = [{"part_number": "NE555P", "quantity": 10}]
        restore = _as_user(client, owner)
        try:
            resp = client.post(f"/api/resell/{el.id}/import-confirm", data={"rows_json": json.dumps(rows)})
        finally:
            restore()
        assert resp.status_code == 200
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert trigger["showToast"]["type"] == "success"
        assert "Imported 1 line" in trigger["showToast"]["message"]

    def test_template_has_no_client_side_success_toast(self):
        source = open("app/templates/htmx/partials/resell/import_preview.html").read()
        assert "Imported {{ valid_count }}" not in source


# ── #31: import-confirm payload shape guard ──────────────────────────


class TestImportConfirmShapeGuard:
    @pytest.mark.parametrize("payload", ["5", '["x"]', '{"a": 1}'])
    def test_malformed_payload_shapes_400(self, client, db_session, owner, test_company, payload):
        el = _make_list(db_session, owner, test_company, lines=0)
        restore = _as_user(client, owner)
        try:
            resp = client.post(f"/api/resell/{el.id}/import-confirm", data={"rows_json": payload})
        finally:
            restore()
        assert resp.status_code == 400


# ── #34: import-preview draft-only 409 ───────────────────────────────


class TestImportPreviewDraftGuard:
    def test_preview_on_posted_list_409s_before_parsing(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN)
        restore = _as_user(client, owner)
        try:
            resp = client.post(
                f"/api/resell/{el.id}/import-preview",
                files={"file": ("x.csv", b"part_number,quantity\nLM358N,5\n", "text/csv")},
            )
        finally:
            restore()
        assert resp.status_code == 409

    def test_preview_on_draft_still_works(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.DRAFT)
        restore = _as_user(client, owner)
        try:
            resp = client.post(
                f"/api/resell/{el.id}/import-preview",
                files={"file": ("x.csv", b"part_number,quantity\nLM358N,5\n", "text/csv")},
            )
        finally:
            restore()
        assert resp.status_code == 200
        # The preview grid must actually render the parsed row, not just 200.
        assert "LM358N" in resp.text
        assert "Confirm import" in resp.text


# ── #17: rows-only search swap ───────────────────────────────────────


class TestSearchRowsOnlySwap:
    def test_search_input_targets_rows_container(self, client, db_session, owner, test_company):
        _make_list(db_session, owner, test_company, title="Rows swap")
        restore = _as_user(client, owner)
        try:
            body = client.get("/v2/partials/resell/lists?lens=mine").text
        finally:
            restore()
        # The input targets the rows container, and the rows container exists.
        assert 'id="resell-list-rows"' in body
        input_chunk = body.split('type="search"')[1][:600]
        assert 'hx-target="#resell-list-rows"' in input_chunk
        assert "/v2/partials/resell/list-rows" in input_chunk

    def test_rows_endpoint_returns_rows_without_filter_bar(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, title="Rows only")
        restore = _as_user(client, owner)
        try:
            body = client.get("/v2/partials/resell/list-rows?lens=mine").text
        finally:
            restore()
        assert "Rows only" in body
        assert 'type="search"' not in body  # the filter bar stays outside the swap
        assert el is not None

    def test_rows_endpoint_search_filters(self, client, db_session, owner, test_company):
        _make_list(db_session, owner, test_company, title="Alpha widgets")
        _make_list(db_session, owner, test_company, title="Beta gadgets")
        restore = _as_user(client, owner)
        try:
            body = client.get("/v2/partials/resell/list-rows?lens=mine&q=Alpha").text
        finally:
            restore()
        assert "Alpha widgets" in body
        assert "Beta gadgets" not in body


# ── perf (b): left-list cap + load-more ──────────────────────────────


class TestListCapLoadMore:
    def test_list_caps_rows_and_offers_load_more(self, client, db_session, owner, test_company):
        from app.routers import resell as resell_router

        cap = resell_router._LIST_PAGE_SIZE
        for i in range(cap + 3):
            _make_list(db_session, owner, test_company, title=f"Cap list {i:03d}", lines=0)
        restore = _as_user(client, owner)
        try:
            body = client.get("/v2/partials/resell/lists?lens=mine").text
        finally:
            restore()
        assert body.count("Cap list ") == cap
        assert "Load more" in body
        assert f"offset={cap}" in body

    def test_load_more_page_returns_remainder_without_button(self, client, db_session, owner, test_company):
        from app.routers import resell as resell_router

        cap = resell_router._LIST_PAGE_SIZE
        for i in range(cap + 3):
            _make_list(db_session, owner, test_company, title=f"Page list {i:03d}", lines=0)
        restore = _as_user(client, owner)
        try:
            body = client.get(f"/v2/partials/resell/list-rows?lens=mine&offset={cap}").text
        finally:
            restore()
        assert body.count("Page list ") == 3
        assert "Load more" not in body

    def test_stale_load_more_page_never_renders_empty_state(self, client, db_session, owner, test_company):
        """A Load-more click that races a shrink (rows deleted since the button
        rendered) returns an offset page with zero rows — it must render NOTHING, not
        the workspace empty-state ("You haven't posted…" + Post CTA) beneath the rows
        already on screen."""
        from app.routers import resell as resell_router

        cap = resell_router._LIST_PAGE_SIZE
        _make_list(db_session, owner, test_company, title="Lone list", lines=0)
        restore = _as_user(client, owner)
        try:
            body = client.get(f"/v2/partials/resell/list-rows?lens=mine&offset={cap}").text
        finally:
            restore()
        assert "You haven't posted" not in body
        assert "Post a list" not in body
        assert "No lists match" not in body

    def test_ordering_is_updated_at_desc_across_pages(self, client, db_session, owner, test_company):
        from app.routers import resell as resell_router

        cap = resell_router._LIST_PAGE_SIZE
        now = datetime.now(UTC)
        for i in range(cap + 2):
            el = _make_list(db_session, owner, test_company, title=f"Ord list {i:03d}", lines=0)
            el.updated_at = now - timedelta(minutes=i)
        db_session.commit()
        restore = _as_user(client, owner)
        try:
            page1 = client.get("/v2/partials/resell/list-rows?lens=mine").text
            page2 = client.get(f"/v2/partials/resell/list-rows?lens=mine&offset={cap}").text
        finally:
            restore()
        assert "Ord list 000" in page1  # newest first
        assert f"Ord list {cap:03d}" in page2  # oldest lands on page 2
        assert f"Ord list {cap + 1:03d}" in page2


# ── #18: offer-count chip filtered to visible statuses ───────────────


class TestVisibleOfferChipCount:
    def test_withdrawn_offer_drops_out_of_chip_count(self, client, db_session, owner, broker, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.COLLECTING)
        for status in (ExcessOfferStatus.OPEN, ExcessOfferStatus.WITHDRAWN, ExcessOfferStatus.WITHDRAWN):
            db_session.add(
                ExcessOffer(
                    excess_list_id=el.id,
                    submitted_by=broker.id,
                    scope=ExcessOfferScope.TAKE_ALL,
                    status=status,
                )
            )
        db_session.commit()
        restore = _as_user(client, owner)
        try:
            body = client.get(f"/v2/partials/resell/{el.id}").text
        finally:
            restore()
        assert "1 offer" in body
        assert "3 offers" not in body


# ── #20: close_at tz-offset conversion ───────────────────────────────


class TestParseCloseAtTz:
    def test_offset_converts_wall_clock_to_utc(self):
        from app.routers.resell import _parse_close_at

        parsed = _parse_close_at("2030-07-24T17:00", "240")  # UTC-4
        assert parsed == datetime(2030, 7, 24, 21, 0, tzinfo=UTC)

    def test_negative_offset_east_of_utc(self):
        from app.routers.resell import _parse_close_at

        parsed = _parse_close_at("2030-07-24T17:00", "-120")  # UTC+2
        assert parsed == datetime(2030, 7, 24, 15, 0, tzinfo=UTC)

    def test_missing_offset_falls_back_to_utc(self):
        from app.routers.resell import _parse_close_at

        parsed = _parse_close_at("2030-07-24T17:00", "")
        assert parsed == datetime(2030, 7, 24, 17, 0, tzinfo=UTC)

    def test_locally_future_utc_past_deadline_now_validates(self, client, db_session, owner, test_company):
        """The spurious-400 repro: a deadline 2h in the local future at UTC-4 whose naive
        wall-clock is already past in UTC must pass validation once the offset is applied."""
        local_now_minus = datetime.now(UTC) - timedelta(hours=2)  # naive-as-UTC would be past
        wall_clock = local_now_minus.strftime("%Y-%m-%dT%H:%M")
        restore = _as_user(client, owner)
        try:
            resp = client.post(
                "/api/resell/lists",
                data={
                    "title": "TZ list",
                    "company_id": str(test_company.id),
                    "close_at": wall_clock,
                    "tz_offset_min": "240",  # UTC-4: +240min → ~2h in the future
                },
            )
        finally:
            restore()
        assert resp.status_code == 200, resp.text
        el = db_session.query(ExcessList).filter_by(title="TZ list").one()
        assert el.close_at is not None

    def test_create_modal_carries_tz_offset_input(self, client, db_session, owner):
        restore = _as_user(client, owner)
        try:
            body = client.get("/v2/partials/resell/create-form").text
        finally:
            restore()
        assert 'name="tz_offset_min"' in body
        assert "getTimezoneOffset" in body


# ── #50: can_offer posted-status gate ────────────────────────────────


class TestCanOfferPostedGate:
    def _detail_ctx(self, db, el, user):
        from unittest.mock import MagicMock

        from app.routers.resell import _detail_context

        request = MagicMock()
        return _detail_context(request, db, el, user)

    def test_broker_on_closed_list_cannot_offer(self, db_session, owner, broker, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.CLOSED)
        assert self._detail_ctx(db_session, el, broker)["can_offer"] is False

    def test_broker_on_expired_list_cannot_offer(self, db_session, owner, broker, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.EXPIRED)
        assert self._detail_ctx(db_session, el, broker)["can_offer"] is False

    def test_broker_on_open_list_can_offer(self, db_session, owner, broker, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN)
        assert self._detail_ctx(db_session, el, broker)["can_offer"] is True


# ── #51: empty-state branch order ────────────────────────────────────


class TestEmptyStateBranchOrder:
    def test_open_lens_with_filter_shows_filter_copy(self, client, db_session, broker):
        restore = _as_user(client, broker)
        try:
            body = client.get("/v2/partials/resell/lists?lens=open&q=zzz-no-match").text
        finally:
            restore()
        assert "No lists match this filter." in body
        assert "No postings are open to you" not in body

    def test_open_lens_unfiltered_empty_shows_open_copy(self, client, db_session, broker):
        restore = _as_user(client, broker)
        try:
            body = client.get("/v2/partials/resell/lists?lens=open").text
        finally:
            restore()
        assert "No postings are open to you" in body


# ── #33/42: excess_service.add_line ──────────────────────────────────


class TestAddLineService:
    def test_add_line_creates_and_bumps_counter(self, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, lines=0)
        result = excess_service.add_line(
            db_session,
            el.id,
            owner,
            part_number="  LM358N ",
            quantity=5,
            manufacturer="TI",
            condition="New",
            date_code="2024",
            asking_price=Decimal("0.45"),
        )
        line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()
        assert line.part_number == "LM358N"  # stripped
        assert line.normalized_part_number == normalize_mpn_key("LM358N")
        assert result.total_line_items == 1

    def test_blank_part_number_400(self, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, lines=0)
        with pytest.raises(HTTPException) as exc:
            excess_service.add_line(db_session, el.id, owner, part_number="   ", quantity=5)
        assert exc.value.status_code == 400

    def test_non_positive_quantity_400(self, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, lines=0)
        with pytest.raises(HTTPException) as exc:
            excess_service.add_line(db_session, el.id, owner, part_number="LM358N", quantity=0)
        assert exc.value.status_code == 400

    def test_non_owner_403(self, db_session, owner, broker, test_company):
        el = _make_list(db_session, owner, test_company, lines=0)
        with pytest.raises(HTTPException) as exc:
            excess_service.add_line(db_session, el.id, broker, part_number="LM358N", quantity=5)
        assert exc.value.status_code == 403

    def test_posted_list_409(self, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN, lines=0)
        with pytest.raises(HTTPException) as exc:
            excess_service.add_line(db_session, el.id, owner, part_number="LM358N", quantity=5)
        assert exc.value.status_code == 409

    def test_route_delegates_whitespace_part_number_400(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, lines=0)
        restore = _as_user(client, owner)
        try:
            resp = client.post(
                f"/api/resell/{el.id}/lines",
                data={"part_number": "   ", "quantity": "5"},
            )
        finally:
            restore()
        assert resp.status_code == 400


# ── #40: header edit preserves customer_site_id ──────────────────────


class TestSiteIdPreserved:
    def test_title_only_update_preserves_site_id(self, db_session, owner, test_company):
        from app.models.crm import CustomerSite

        site = CustomerSite(company_id=test_company.id, site_name="DGHI HQ")
        db_session.add(site)
        db_session.flush()
        el = _make_list(db_session, owner, test_company, lines=0)
        el.customer_site_id = site.id
        db_session.commit()
        excess_service.update_excess_list(db_session, el.id, owner, title="Renamed only")
        db_session.refresh(el)
        assert el.customer_site_id == site.id


# ── #41: confirm_import takes the acting user ────────────────────────


class TestConfirmImportGuards:
    def test_non_owner_direct_call_403(self, db_session, owner, broker, test_company):
        el = _make_list(db_session, owner, test_company, lines=0)
        with pytest.raises(HTTPException) as exc:
            excess_service.confirm_import(db_session, el.id, broker, [{"part_number": "X1", "quantity": 1}])
        assert exc.value.status_code == 403

    def test_posted_list_direct_call_409(self, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN, lines=0)
        with pytest.raises(HTTPException) as exc:
            excess_service.confirm_import(db_session, el.id, owner, [{"part_number": "X1", "quantity": 1}])
        assert exc.value.status_code == 409


# ── #48: foreign-draft 404-mask sweep ────────────────────────────────


class TestForeignDraft404Mask:
    def test_owner_only_endpoints_404_a_foreign_draft(self, client, db_session, owner, broker, test_company):
        """Every owner-only endpoint must 404 (not 403) a foreign private draft so
        sequential-id probing cannot map other users' drafts."""
        el = _make_list(db_session, owner, test_company)  # draft owned by `owner`
        restore = _as_user(client, broker)
        probes = [
            ("GET", f"/v2/partials/resell/{el.id}/build-bid", {}),
            ("POST", f"/api/resell/{el.id}/bid", {"data": {"selections_json": "[]"}}),
            ("GET", f"/api/resell/{el.id}/bid/1/pdf", {}),
            ("POST", f"/api/resell/{el.id}/lines", {"data": {"part_number": "X", "quantity": "1"}}),
            ("POST", f"/api/resell/{el.id}/import-preview", {"files": {"file": ("x.csv", b"a,b\n", "text/csv")}}),
            ("POST", f"/api/resell/{el.id}/import-confirm", {"data": {"rows_json": "[]"}}),
            ("POST", f"/api/resell/{el.id}/publish", {}),
            ("GET", f"/v2/partials/resell/{el.id}/offer-buyers-form", {}),
            ("GET", f"/v2/partials/resell/{el.id}/outreach", {}),
            ("GET", f"/v2/partials/resell/{el.id}/outreach/export", {}),
            ("GET", f"/v2/partials/resell/{el.id}/not-yet-strip", {}),
            ("POST", f"/api/resell/{el.id}/outreach", {"data": {"vendor_card_ids": "1", "channel": "phone"}}),
            ("POST", f"/api/resell/{el.id}/outreach/1/retry", {}),
            ("GET", f"/v2/partials/resell/{el.id}/outreach/1/reply", {}),
            ("POST", f"/api/resell/{el.id}/outreach/1/log-response", {}),
        ]
        try:
            for method, url, kwargs in probes:
                resp = client.request(method, url, **kwargs)
                assert resp.status_code == 404, f"{method} {url} → {resp.status_code} (expected 404)"
        finally:
            restore()


# ── #6: nudge headroom ───────────────────────────────────────────────


class TestNudgeHeadroom:
    def test_strip_survives_a_big_campaign(self, db_session, owner, test_company, monkeypatch):
        """25 history buyers, top-20 already campaigned → the strip must surface the
        remaining 5, not go empty (the pre-fix slice starved it)."""
        from app.models import VendorCard
        from app.services import buyer_affinity_service as bas

        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN, lines=1)
        cards = []
        for i in range(25):
            card = VendorCard(normalized_name=f"nudge-buyer-{i}", display_name=f"Nudge Buyer {i}")
            db_session.add(card)
            db_session.flush()
            cards.append(card)
        db_session.commit()

        ranked = [
            bas.RankedBuyer(
                vendor_card_id=c.id,
                display_name=c.display_name,
                rank_reason="bought_this_part",
                has_contact=True,
                engagement_score=None,
                last_bid=None,
                win_rate=None,
                last_offered_at=None,
            )
            for c in cards
        ]

        def fake_rank(db, *, excess_list_id=None, line_item_ids=None, limit=20):
            return ranked[:limit]

        monkeypatch.setattr(bas, "rank_buyers_for", fake_rank)
        # Top 20 already touched on this list.
        for c in cards[:20]:
            db_session.add(
                ExcessOutreach(
                    excess_list_id=el.id,
                    target_vendor_card_id=c.id,
                    submitted_by=owner.id,
                    channel=ExcessOutreachChannel.PHONE,
                    status=ExcessOutreachStatus.SENT,
                )
            )
        db_session.commit()

        strip = bas.not_yet_offered_strip(db_session, excess_list_id=el.id, limit=20)
        got = {r.vendor_card_id for r in strip}
        assert got == {c.id for c in cards[20:]}


# ── #35/49: buyer-panel line scoping ─────────────────────────────────


class TestBuyerPanelLineScope:
    def test_foreign_line_id_422(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN, lines=1)
        foreign = _make_list(db_session, owner, test_company, status=ExcessListStatus.DRAFT, lines=1, title="Foreign")
        foreign_line = db_session.query(ExcessLineItem).filter_by(excess_list_id=foreign.id).one()
        restore = _as_user(client, owner)
        try:
            resp = client.get(f"/v2/partials/resell/{el.id}/offer-buyers-form?line_ids={foreign_line.id}")
        finally:
            restore()
        assert resp.status_code == 422

    def test_own_line_ids_unchanged(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN, lines=2)
        own_line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        restore = _as_user(client, owner)
        try:
            resp = client.get(f"/v2/partials/resell/{el.id}/offer-buyers-form?line_ids={own_line.id}")
        finally:
            restore()
        assert resp.status_code == 200
        assert "1 selected line" in resp.text  # the scoped line landed in the panel


# ── perf (e): offer-compare direct query ─────────────────────────────


class TestOfferCompareDirectQuery:
    def test_compare_filters_scope_and_status(self, client, db_session, owner, broker, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.COLLECTING, lines=1)
        line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()

        def _offer(status, scope=ExcessOfferScope.PER_LINE, price="1.00"):
            o = ExcessOffer(excess_list_id=el.id, submitted_by=broker.id, scope=scope, status=status)
            db_session.add(o)
            db_session.flush()
            if scope == ExcessOfferScope.PER_LINE:
                db_session.add(
                    ExcessOfferLine(
                        offer_id=o.id,
                        excess_line_item_id=line.id,
                        mpn_raw=line.part_number,
                        quantity=5,
                        unit_price=Decimal(price),
                        match_status="matched",
                    )
                )
            return o

        visible = _offer(ExcessOfferStatus.OPEN, price="2.00")
        _offer(ExcessOfferStatus.WITHDRAWN, price="9.00")  # hidden status
        _offer(ExcessOfferStatus.WITHDRAWN, price="8.00")  # hidden status
        db_session.commit()

        restore = _as_user(client, owner)
        try:
            resp = client.get(f"/v2/partials/resell/{el.id}/lines/{line.id}/offers")
        finally:
            restore()
        assert resp.status_code == 200
        assert "2.00" in resp.text
        assert "9.00" not in resp.text
        assert "8.00" not in resp.text
        assert visible is not None

    def test_compare_never_lazy_loads_all_offers(self, client, db_session, owner, broker, test_company):
        """The compare render queries offer-lines for THIS line directly — el.offers
        must not be walked (its lazy loader would drag every offer/line on the list)."""
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.COLLECTING, lines=1)
        line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()
        db_session.commit()
        import re as _re

        source = open("app/routers/resell.py").read()
        compare_fn = source.split("def resell_line_offer_compare")[1].split("\nasync def ")[0].split("\ndef ")[0]
        assert not _re.search(r"\bel\.offers\b", compare_fn)
        restore = _as_user(client, owner)
        try:
            resp = client.get(f"/v2/partials/resell/{el.id}/lines/{line.id}/offers")
        finally:
            restore()
        assert resp.status_code == 200


# ── B3: degraded email row renders Log actions ───────────────────────


class TestDegradedRowLogActions:
    def _outreach_row(self, db, el, owner, *, channel, status, conv_id=None):
        row = ExcessOutreach(
            excess_list_id=el.id,
            submitted_by=owner.id,
            channel=channel,
            status=status,
            graph_conversation_id=conv_id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def test_degraded_email_row_shows_log_actions(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN)
        row = self._outreach_row(
            db_session, el, owner, channel=ExcessOutreachChannel.EMAIL, status=ExcessOutreachStatus.SENT
        )
        restore = _as_user(client, owner)
        try:
            body = client.get(f"/v2/partials/resell/{el.id}/outreach").text
        finally:
            restore()
        assert f"/outreach/{row.id}/log-response" in body
        assert f"/outreach/{row.id}/log-bid-form" in body
        assert "View reply" not in body

    def test_threaded_email_row_keeps_only_view_reply(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN)
        row = self._outreach_row(
            db_session,
            el,
            owner,
            channel=ExcessOutreachChannel.EMAIL,
            status=ExcessOutreachStatus.RESPONDED,
            conv_id="conv-123",
        )
        restore = _as_user(client, owner)
        try:
            body = client.get(f"/v2/partials/resell/{el.id}/outreach").text
        finally:
            restore()
        assert "View reply" in body
        assert f"/outreach/{row.id}/log-bid-form" not in body
        assert f"/outreach/{row.id}/log-response" not in body

    def test_manual_channel_row_keeps_log_actions(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN)
        row = self._outreach_row(
            db_session, el, owner, channel=ExcessOutreachChannel.PHONE, status=ExcessOutreachStatus.SENT
        )
        restore = _as_user(client, owner)
        try:
            body = client.get(f"/v2/partials/resell/{el.id}/outreach").text
        finally:
            restore()
        assert f"/outreach/{row.id}/log-response" in body
        assert f"/outreach/{row.id}/log-bid-form" in body

    def test_terminal_degraded_row_hides_log_actions(self, client, db_session, owner, test_company):
        el = _make_list(db_session, owner, test_company, status=ExcessListStatus.OPEN)
        row = self._outreach_row(
            db_session, el, owner, channel=ExcessOutreachChannel.EMAIL, status=ExcessOutreachStatus.BID
        )
        restore = _as_user(client, owner)
        try:
            body = client.get(f"/v2/partials/resell/{el.id}/outreach").text
        finally:
            restore()
        assert f"/outreach/{row.id}/log-bid-form" not in body


# ── perf (c): modal company selects load (id, name) only ─────────────


class TestCompanySelectSlim:
    def test_create_form_renders_company_options(self, client, db_session, owner, test_company):
        restore = _as_user(client, owner)
        try:
            body = client.get("/v2/partials/resell/create-form").text
        finally:
            restore()
        assert f'value="{test_company.id}"' in body
        assert test_company.name in body

    def test_router_never_loads_full_company_entities(self):
        source = open("app/routers/resell.py").read()
        assert "db.query(Company).order_by" not in source
        assert "select(Company).order_by" not in source

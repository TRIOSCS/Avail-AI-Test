"""test_resell_routes.py — Route/render tests for the Resell workspace (Chunk F).

Exercises the NEW additive endpoints end-to-end with the TestClient: each returns
200 + the right partial for a seeded list; an offer submit creates an ExcessOffer;
and the offerer-facing list view (the "Open to Me" lens) omits the customer name.
The old excess routes/tests are untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOfferScope
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer
from app.utils.normalization import normalize_mpn_key


@pytest.fixture()
def trader_user(db_session: Session) -> User:
    """The list owner — a trader (can_post + can_offer)."""
    user = User(
        email="trader@trioscs.com",
        name="Tess Trader",
        role="trader",
        azure_id="test-azure-trader",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def posted_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A posted (collecting) list owned by the trader, with two priced lines."""
    el = ExcessList(
        title="Acme surplus",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=2,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(el)
    db_session.flush()
    for mpn in ("XCVU9P-2FLGA2104I", "EP4CE10F17C8N"):
        db_session.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number=mpn,
                normalized_part_number=normalize_mpn_key(mpn),
                quantity=100,
                condition="New",
            )
        )
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def single_line_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A one-off list (one line) owned by the trader — exercises the single-card
    shape."""
    el = ExcessList(
        title="One-off heatsink",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.OPEN,
        total_line_items=1,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="DELL-412-AAVE",
            normalized_part_number=normalize_mpn_key("DELL-412-AAVE"),
            quantity=24,
        )
    )
    db_session.commit()
    db_session.refresh(el)
    return el


# ── Workspace + lists ────────────────────────────────────────────────


def test_workspace_renders(client, trader_user, posted_list):
    """The split-panel shell renders with the lens pills + stat strip."""
    # client's overridden user is the buyer fixture; the route still renders.
    resp = client.get("/v2/partials/resell/workspace")
    assert resp.status_code == 200
    body = resp.text
    assert "My Lists" in body
    assert "Open to Me" in body
    assert "split-resell" in body  # splitPanel container


def test_create_form_route_not_shadowed_by_list_id(client, trader_user):
    """Regression: the static /v2/partials/resell/create-form route must be matched as itself.

    Before the fix, the dynamic /{list_id} route was registered first, so FastAPI matched
    'create-form' against {list_id} and returned 422 (int-parse on list_id) — the 'New List'
    button was dead. Override require_user to a can_post trader and assert the modal renders.
    """
    from app.dependencies import require_user
    from app.main import app

    prev = app.dependency_overrides.get(require_user)
    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get("/v2/partials/resell/create-form")
    finally:
        if prev is not None:
            app.dependency_overrides[require_user] = prev
        else:
            app.dependency_overrides.pop(require_user, None)

    assert resp.status_code != 422, f"create-form shadowed by /{{list_id}}: {resp.text}"
    assert resp.status_code == 200, resp.text  # trader can_post → the new-list modal renders


def test_full_page_route(client, trader_user):
    """/v2/resell serves the base shell, wired to load the workspace partial.

    v2_page authenticates via the session-based get_user (not the Depends-injected
    require_user), so we patch that helper — the established pattern for shell tests.
    """
    from unittest.mock import patch

    with patch("app.routers.htmx_views.get_user", return_value=trader_user):
        resp = client.get("/v2/resell")
    assert resp.status_code == 200
    assert "/v2/partials/resell/workspace" in resp.text


def test_lists_mine_shows_customer(client, db_session, trader_user, posted_list):
    """My-Lists lens (owner view) shows the seller company name."""
    # Make the overridden client user the owner so 'mine' returns the list.
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get("/v2/partials/resell/lists?lens=mine")
        assert resp.status_code == 200
        assert posted_list.title in resp.text
        assert "Acme Electronics" in resp.text  # test_company name visible to owner
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_lists_open_lens_hides_customer(client, db_session, trader_user, posted_list):
    """Open-to-Me lens (offerer view) lists the posting but NEVER the seller name —
    including via the owner's free-text title (finding H2)."""
    # The default client user is the buyer fixture (!= owner) → sees it under 'open'.
    resp = client.get("/v2/partials/resell/lists?lens=open")
    assert resp.status_code == 200
    body = resp.text
    assert posted_list.title not in body  # raw free-text title is anonymized, not leaked
    assert f"Excess listing #{posted_list.id}" in body  # neutral, id-derived label instead
    assert "Acme Electronics" not in body  # customer hidden from non-owner
    assert "Anonymized" in body


def test_open_lens_title_never_leaks_customer_via_free_text(client, db_session, trader_user, test_company):
    """H2: a trader who names a list after the customer must not leak that name to offerers.

    Non-owners (the open lens + the non-owner detail) get a neutral "Excess listing #N"
    label; the owner still sees the real free-text title in both the mine lens and detail.
    Proves the anonymization gate now covers the one field it used to miss — the title.
    """
    from app.dependencies import require_user
    from app.main import app

    # Title deliberately carries the customer's company name — the natural trader habit.
    leaky_title = f"{test_company.name} — surplus FPGAs Q3"
    el = ExcessList(
        title=leaky_title,
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.OPEN,
        total_line_items=1,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="XCVU9P-2FLGA2104I",
            normalized_part_number=normalize_mpn_key("XCVU9P-2FLGA2104I"),
            quantity=50,
            condition="New",
        )
    )
    db_session.commit()
    db_session.refresh(el)
    neutral = f"Excess listing #{el.id}"

    # ── Non-owner (default buyer client): open lens + detail hide the title. ──
    open_body = client.get("/v2/partials/resell/lists?lens=open").text
    assert leaky_title not in open_body
    assert test_company.name not in open_body
    assert neutral in open_body

    detail_body = client.get(f"/v2/partials/resell/{el.id}").text
    assert leaky_title not in detail_body
    assert test_company.name not in detail_body
    assert neutral in detail_body

    # ── Owner: the real free-text title is still shown (mine lens + detail). ──
    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        mine_body = client.get("/v2/partials/resell/lists?lens=mine").text
        assert leaky_title in mine_body
        owner_detail = client.get(f"/v2/partials/resell/{el.id}").text
        assert leaky_title in owner_detail
    finally:
        app.dependency_overrides.pop(require_user, None)


# ── Detail + tabs ────────────────────────────────────────────────────


def test_detail_renders_tabs(client, trader_user, posted_list):
    """Detail renders the breadcrumb + chips + the three core tabs.

    The Activity tab was removed per the 2026-06-24 UI-review audit which flagged it as
    a permanent dead-end 'coming soon' placeholder (no backing route/partial existed).
    The audit recommended hiding it (S option); the feat/ui-light restyle PR removed it.
    This test asserts the tabs that are actually present.
    """
    resp = client.get(f"/v2/partials/resell/{posted_list.id}")
    assert resp.status_code == 200
    body = resp.text
    # Non-owner detail: the breadcrumb "Resell" link renders, but the header shows the
    # anonymized label — never the seller-named free-text title (finding H2).
    assert "Resell" in body
    assert posted_list.title not in body
    assert f"Excess listing #{posted_list.id}" in body
    for label in ("Lines", "Offers", "Build Bid"):
        assert label in body
    # Activity tab intentionally absent (audit-approved removal of dead-end placeholder)
    assert "Activity" not in body


def test_lines_multi_is_table(client, trader_user, posted_list):
    """≥2 lines → compact table shape."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/lines")
    assert resp.status_code == 200
    assert "compact-table" in resp.text


def test_lines_single_is_card(client, trader_user, single_line_list):
    """Exactly 1 line → single card, no table chrome."""
    resp = client.get(f"/v2/partials/resell/{single_line_list.id}/lines")
    assert resp.status_code == 200
    body = resp.text
    assert "compact-table" not in body
    assert "DELL-412-AAVE" in body


def test_offers_tab_renders(client, trader_user, posted_list):
    """Offers tab renders for the owner."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.pop(require_user, None)


# ── Offer submit (the centerpiece) ───────────────────────────────────


def test_submit_offer_creates_excess_offer(client, db_session, trader_user, posted_list, test_user):
    """A per-line offer submit creates an ExcessOffer (offerer = the buyer client
    user)."""
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={
            "scope": "per_line",
            "mpn_raw": line.part_number,
            "quantity": "40",
            "unit_price": "142.50",
            "lead_time_days": "7",
            "notes": "test offer",
        },
    )
    assert resp.status_code == 200
    offers = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).all()
    assert len(offers) == 1
    assert offers[0].scope == ExcessOfferScope.PER_LINE
    # Rollup recomputed: the matched line now has a best price.
    db_session.refresh(line)
    assert line.offer_count == 1
    assert line.best_offer_unit_price is not None


def test_submit_take_all_offer(client, db_session, trader_user, posted_list):
    """A take-all offer submit creates a take_all-scoped ExcessOffer (no lines)."""
    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "take_all", "take_all_total_price": "48500.00", "notes": "whole lot"},
    )
    assert resp.status_code == 200
    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    assert offer.scope == ExcessOfferScope.TAKE_ALL
    assert offer.take_all_total_price == Decimal("48500.00")
    assert offer.lines == []


def test_self_offer_blocked(client, db_session, trader_user, posted_list):
    """The self-offer guard fires when the list owner tries to offer (403)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user  # the owner
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/offers",
            data={"scope": "per_line", "mpn_raw": "XCVU9P-2FLGA2104I", "quantity": "10"},
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(require_user, None)


# ── Create / add-line / publish ──────────────────────────────────────


def test_create_list(client, db_session, trader_user, test_company):
    """Posting the create form makes a new list owned by the current user."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.post(
            "/api/resell/lists",
            data={"title": "Brand new list", "company_id": str(test_company.id), "notes": "n"},
        )
        assert resp.status_code == 200
        el = db_session.query(ExcessList).filter_by(title="Brand new list").one()
        assert el.owner_id == trader_user.id
        assert el.status == ExcessListStatus.DRAFT
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_add_line_renders_lines(client, db_session, trader_user, draft_list):
    """Adding a line to a DRAFT list returns the re-rendered Lines tab and persists the
    line."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        before = db_session.query(ExcessLineItem).filter_by(excess_list_id=draft_list.id).count()
        resp = client.post(
            f"/api/resell/{draft_list.id}/lines",
            data={"part_number": "LM358N", "quantity": "500", "manufacturer": "TI", "condition": "New"},
        )
        assert resp.status_code == 200
        after = db_session.query(ExcessLineItem).filter_by(excess_list_id=draft_list.id).count()
        assert after == before + 1
        assert "compact-table" in resp.text
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_offer_compare_renders(client, db_session, trader_user, posted_list, test_user):
    """The per-line comparison renders best-highlight markup after an offer lands."""
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    # Submit one offer as the buyer (client default user).
    client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": line.part_number, "quantity": "40", "unit_price": "9.99"},
    )
    # Owner (trader_user) can access the comparison.
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/lines/{line.id}/offers")
        assert resp.status_code == 200
        assert line.part_number in resp.text
        assert "Best" in resp.text
    finally:
        app.dependency_overrides.pop(require_user, None)


# ── Security / authorization tests ───────────────────────────────────


@pytest.fixture()
def draft_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A DRAFT list owned by trader_user — must NOT be visible to anyone else."""
    el = ExcessList(
        title="Private draft",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status="draft",
        total_line_items=1,
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="DRAFT-PART-001",
            quantity=10,
        )
    )
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def empty_draft_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A freshly-created DRAFT list with ZERO lines (the create-modal starting state).

    RS-5 fixture: adding the FIRST line to this list is what should make the header Post
    button appear.
    """
    el = ExcessList(
        title="Fresh empty draft",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status="draft",
        total_line_items=0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


def test_add_line_returns_full_detail_so_post_appears(client, db_session, trader_user, empty_draft_list):
    """RS-5: adding the first line to a draft re-renders the WHOLE detail (not just the
    Lines tab), so the header Post button appears.

    A Lines-only swap (the old behaviour) left the header stale: a freshly-created empty
    list that just got its first lines showed no way to publish. The response must be the
    full detail (its root marker is only in detail.html) AND carry the publish action.
    """
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.post(
            f"/api/resell/{empty_draft_list.id}/lines",
            data={"part_number": "LM358N", "quantity": "500", "condition": "New"},
        )
        assert resp.status_code == 200
        body = resp.text
        # Only detail.html renders the root marker — a Lines-only swap would omit it.
        assert "data-resell-detail-root" in body
        # The header Post (publish) action now shows because the draft has a line.
        assert f"/api/resell/{empty_draft_list.id}/publish" in body
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_import_confirm_returns_full_detail_so_post_appears(client, db_session, trader_user, empty_draft_list):
    """RS-5: confirming an import re-renders the whole detail so the Post button appears."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.post(
            f"/api/resell/{empty_draft_list.id}/import-confirm",
            data={"rows_json": json.dumps([{"part_number": "LM358N", "quantity": 100, "condition": "New"}])},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "data-resell-detail-root" in body
        assert f"/api/resell/{empty_draft_list.id}/publish" in body
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_import_preview_zero_valid_rows_offers_retry(client, db_session, trader_user, empty_draft_list):
    """RS-6: an all-errors import preview still renders a re-upload/back affordance.

    The old preview only rendered the Confirm form when valid_count > 0, so an all-errors
    file left the user stranded inside #import-area (error list only, no re-upload, no
    cancel). The preview must always render a way back to the dropzone.
    """
    from app.dependencies import require_user
    from app.main import app

    csv_bytes = b"part_number,quantity\n,100\n,50\n"  # both rows blank part_number → all invalid
    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.post(
            f"/api/resell/{empty_draft_list.id}/import-preview",
            files={"file": ("bad.csv", csv_bytes, "text/csv")},
        )
        assert resp.status_code == 200
        body = resp.text
        assert "0 valid rows" in body
        assert "Confirm import" not in body  # nothing to confirm
        # A re-upload/back affordance is present and returns to the lines dropzone.
        assert "Try another file" in body
        assert f"/v2/partials/resell/{empty_draft_list.id}/lines" in body
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_offer_buyers_form_preselects_buyer(client, db_session, trader_user, posted_list):
    """RS-8: the offer-to-buyers panel seeds its checked set from preselect_vendor_card_id.

    The "not yet offered" nudge chips promise to pre-fill the buyer; the panel now honours
    a preselect param so the buyer lands already selected (one click from action) instead
    of the generic panel with nothing checked.
    """
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-buyers-form?preselect_vendor_card_id=777")
        assert resp.status_code == 200
        # The Alpine state seeds selected with the preselected card id.
        assert "selected: [777]" in resp.text
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_non_owner_draft_detail_404(client, draft_list, test_user):
    """Non-owner GET on a DRAFT list → 404 (existence not revealed)."""
    # The default client user (test_user, a buyer) is NOT the owner.
    resp = client.get(f"/v2/partials/resell/{draft_list.id}")
    assert resp.status_code == 404


def test_non_owner_draft_lines_404(client, draft_list, test_user):
    """Non-owner GET on draft list's lines tab → 404."""
    resp = client.get(f"/v2/partials/resell/{draft_list.id}/lines")
    assert resp.status_code == 404


def test_non_owner_draft_offers_404(client, draft_list, test_user):
    """Non-owner GET on draft list's offers tab → 404."""
    resp = client.get(f"/v2/partials/resell/{draft_list.id}/offers")
    assert resp.status_code == 404


def test_non_owner_posted_list_200_no_offers_no_customer(client, db_session, posted_list, trader_user, test_user):
    """Non-owner GET on a posted (collecting) list → 200, no offer data, no customer
    name.

    The default client user is test_user (buyer, NOT the owner). The list is in
    COLLECTING status so it is visible, but offers and customer name are withheld.
    """
    # Confirm the default client user is NOT the owner.
    assert test_user.id != trader_user.id

    # Detail is visible (not 404/403).
    resp = client.get(f"/v2/partials/resell/{posted_list.id}")
    assert resp.status_code == 200
    # Customer name must not leak to non-owner.
    assert "Acme Electronics" not in resp.text

    # Offers tab: 200 but no offer payloads.
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers")
    assert resp.status_code == 200
    # The is_owner=False path renders an empty offers view — no offer rows.
    # The _offers.html template does not render offer rows when is_owner is False.
    assert "Acme Electronics" not in resp.text


def test_non_owner_add_line_403(client, posted_list, test_user):
    """Non-owner POST add-line → 403."""
    resp = client.post(
        f"/api/resell/{posted_list.id}/lines",
        data={"part_number": "HACK-001", "quantity": "1"},
    )
    assert resp.status_code == 403


def test_non_owner_import_confirm_403(client, posted_list, test_user):
    """Non-owner POST import-confirm → 403."""
    resp = client.post(
        f"/api/resell/{posted_list.id}/import-confirm",
        data={"rows_json": json.dumps([{"part_number": "HACK-002", "quantity": 1}])},
    )
    assert resp.status_code == 403


def test_non_owner_line_offer_compare_403(client, db_session, posted_list, test_user):
    """Non-owner GET line-offer-compare → 403 (comparison is owner-only)."""
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/lines/{line.id}/offers")
    assert resp.status_code == 403


def test_owner_draft_detail_200(client, db_session, draft_list, trader_user):
    """Owner GET on their own DRAFT list → 200 (owner always passes)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/resell/{draft_list.id}")
        assert resp.status_code == 200
        assert "Private draft" in resp.text
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_owner_offers_tab_full_data(client, db_session, posted_list, trader_user, test_user):
    """Owner GET on offers tab → 200 with full offer stack visible (regression guard).

    The owner sees the per-line offer table with unit prices and broker labels. Non-
    owners see only the "offers are private" message — the owner view must render the
    actual offer rows, not that message.
    """
    from app.dependencies import require_user
    from app.main import app

    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    # Submit an offer as the buyer (non-owner client).
    client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": line.part_number, "quantity": "10", "unit_price": "5.00"},
    )

    # Now view the offers tab AS THE OWNER.
    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers")
        assert resp.status_code == 200
        body = resp.text
        # Owner sees the offer table — the "offers are private" banner must NOT appear.
        assert "Offers are private to the list owner" not in body
        assert "offers are private" not in body.lower()
        # The submitted part number should appear in the per-line offer table.
        assert line.part_number in body
    finally:
        app.dependency_overrides.pop(require_user, None)


# ── Build Bid tab + bid-back endpoints (Chunk E) ─────────────────────


def _seed_best_price(db_session, posted_list, price="100.0000"):
    """Stamp a best-offer rollup price onto the list's first line (planning seed)."""
    from decimal import Decimal as _D

    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    line.best_offer_unit_price = _D(price)
    line.offer_count = 1
    db_session.commit()
    db_session.refresh(line)
    return line


def test_build_bid_tab_owner_only(client, db_session, trader_user, posted_list, test_user):
    """Non-owner GET on the Build-Bid tab → 403; owner → 200."""
    # Default client user (buyer) is NOT the owner.
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/build-bid")
    assert resp.status_code == 403

    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/build-bid")
        assert resp.status_code == 200
        assert "Assemble bid" in resp.text
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_assemble_bid_creates_customer_bid(client, db_session, trader_user, posted_list):
    """Owner POST assembles a CustomerBid seeded from best price + renders the
    summary."""
    from app.dependencies import require_user
    from app.main import app
    from app.models.excess import CustomerBid

    line = _seed_best_price(db_session, posted_list, "100.0000")

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/bid",
            data={
                "selections_json": json.dumps([{"excess_line_item_id": line.id, "customer_unit_price": ""}]),
            },
        )
        assert resp.status_code == 200
        assert "Download PDF" in resp.text
        bids = db_session.query(CustomerBid).filter_by(excess_list_id=posted_list.id).all()
        assert len(bids) == 1
        assert bids[0].lines[0].customer_unit_price == Decimal("100.0000")
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_assemble_bid_non_owner_403(client, posted_list, test_user):
    """Non-owner POST on the bid endpoint → 403."""
    resp = client.post(
        f"/api/resell/{posted_list.id}/bid",
        data={"selections_json": json.dumps([{"excess_line_item_id": 1}])},
    )
    assert resp.status_code == 403


def test_bid_pdf_download_owner_only(client, db_session, trader_user, posted_list, test_user, monkeypatch):
    """The bid PDF download is owner-only and returns a PDF; non-owner → 403.

    WeasyPrint is stubbed so the assertion is deterministic regardless of renderer/ co-
    runner state — the test verifies the owner-only gate + PDF response wiring.
    """
    from app.dependencies import require_user
    from app.main import app
    from app.services import bid_back_service

    class _FakeHTML:
        def __init__(self, *, string):
            self._string = string

        def write_pdf(self):
            return b"%PDF-1.4 stub"

    import weasyprint

    monkeypatch.setattr(weasyprint, "HTML", _FakeHTML)

    line = _seed_best_price(db_session, posted_list, "55.0000")
    bid = bid_back_service.build_bid_back(
        db_session, list_id=posted_list.id, owner=trader_user, selections=[{"excess_line_item_id": line.id}]
    )

    # Non-owner blocked.
    resp = client.get(f"/api/resell/{posted_list.id}/bid/{bid.id}/pdf")
    assert resp.status_code == 403

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/api/resell/{posted_list.id}/bid/{bid.id}/pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content[:4] == b"%PDF"
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_close_endpoint_owner_only(client, db_session, trader_user, posted_list, test_user):
    """The close endpoint is owner-only; owner close stamps close_at + bid_out
    status."""
    # Non-owner blocked.
    resp = client.post(f"/api/resell/{posted_list.id}/close")
    assert resp.status_code == 403

    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.post(f"/api/resell/{posted_list.id}/close")
        assert resp.status_code == 200
        db_session.refresh(posted_list)
        assert posted_list.status == ExcessListStatus.BID_OUT
        assert posted_list.close_at is not None
    finally:
        app.dependency_overrides.pop(require_user, None)


# ── Lock-on-post guard tests (Chunk D) ───────────────────────────────


def test_add_line_to_posted_list_returns_409(client, db_session, trader_user, posted_list):
    """Owner POST add-line to a posted (non-DRAFT) list → 409 lock guard."""
    from app.dependencies import require_user
    from app.main import app

    assert posted_list.status != ExcessListStatus.DRAFT  # posted_list is COLLECTING

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/lines",
            data={"part_number": "LOCK-TEST-001", "quantity": "10"},
        )
        assert resp.status_code == 409
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_add_line_to_draft_list_works(client, db_session, trader_user, draft_list):
    """Owner POST add-line to a DRAFT list → 200 (lock guard does not block)."""
    from app.dependencies import require_user
    from app.main import app

    assert draft_list.status == ExcessListStatus.DRAFT

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        before = db_session.query(ExcessLineItem).filter_by(excess_list_id=draft_list.id).count()
        resp = client.post(
            f"/api/resell/{draft_list.id}/lines",
            data={"part_number": "DRAFT-ADD-002", "quantity": "5"},
        )
        assert resp.status_code == 200
        after = db_session.query(ExcessLineItem).filter_by(excess_list_id=draft_list.id).count()
        assert after == before + 1
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_import_confirm_to_posted_list_returns_409(client, db_session, trader_user, posted_list):
    """Owner POST import-confirm to a posted list → 409 lock guard."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/import-confirm",
            data={"rows_json": json.dumps([{"part_number": "HACK-003", "quantity": 1}])},
        )
        assert resp.status_code == 409
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_detail_action_buttons_target_self_not_workspace_shell(client, db_session, trader_user, posted_list):
    """RS-2: on a deep-linked/reloaded /v2/resell/{id}, the detail loads standalone
    into #main-content — but Post/Close targeted #split-right-resell, which only exists
    in the workspace shell, so they fired htmx:targetError and did nothing. They must
    target the detail's own always-present root instead."""
    from app.dependencies import require_user

    client.app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}")
    finally:
        client.app.dependency_overrides.pop(require_user, None)
    assert resp.status_code == 200
    body = resp.text
    # The owner sees the Close action (collecting list); it targets the detail root,
    # and no action targets the workspace-shell-only #split-right-resell.
    assert "data-resell-detail-root" in body
    assert "#split-right-resell" not in body
    assert "closest [data-resell-detail-root]" in body

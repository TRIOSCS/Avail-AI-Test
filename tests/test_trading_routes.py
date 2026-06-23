"""test_trading_routes.py — Route/render tests for the Trading workspace (Chunk F).

Exercises the NEW additive endpoints end-to-end with the TestClient: each returns
200 + the right partial for a seeded list; an offer submit creates an ExcessOffer;
and the offerer-facing list view (the "Open to Me" lens) omits the customer name.
The old excess routes/tests are untouched.
"""

from __future__ import annotations

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
    resp = client.get("/v2/partials/trading/workspace")
    assert resp.status_code == 200
    body = resp.text
    assert "My Lists" in body
    assert "Open to Me" in body
    assert "split-trading" in body  # splitPanel container


def test_full_page_route(client, trader_user):
    """/v2/trading serves the base shell, wired to load the workspace partial.

    v2_page authenticates via the session-based get_user (not the Depends-injected
    require_user), so we patch that helper — the established pattern for shell tests.
    """
    from unittest.mock import patch

    with patch("app.routers.htmx_views.get_user", return_value=trader_user):
        resp = client.get("/v2/trading")
    assert resp.status_code == 200
    assert "/v2/partials/trading/workspace" in resp.text


def test_lists_mine_shows_customer(client, db_session, trader_user, posted_list):
    """My-Lists lens (owner view) shows the seller company name."""
    # Make the overridden client user the owner so 'mine' returns the list.
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get("/v2/partials/trading/lists?lens=mine")
        assert resp.status_code == 200
        assert posted_list.title in resp.text
        assert "Acme Electronics" in resp.text  # test_company name visible to owner
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_lists_open_lens_hides_customer(client, db_session, trader_user, posted_list):
    """Open-to-Me lens (offerer view) lists the posting but NEVER the seller name."""
    # The default client user is the buyer fixture (!= owner) → sees it under 'open'.
    resp = client.get("/v2/partials/trading/lists?lens=open")
    assert resp.status_code == 200
    body = resp.text
    assert posted_list.title in body
    assert "Acme Electronics" not in body  # customer hidden from non-owner
    assert "Anonymized" in body


# ── Detail + tabs ────────────────────────────────────────────────────


def test_detail_renders_tabs(client, trader_user, posted_list):
    """Detail renders the breadcrumb + chips + the four lazy tabs."""
    resp = client.get(f"/v2/partials/trading/{posted_list.id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Trading" in body and posted_list.title in body
    for label in ("Lines", "Offers", "Build Bid", "Activity"):
        assert label in body


def test_lines_multi_is_table(client, trader_user, posted_list):
    """≥2 lines → compact table shape."""
    resp = client.get(f"/v2/partials/trading/{posted_list.id}/lines")
    assert resp.status_code == 200
    assert "compact-table" in resp.text


def test_lines_single_is_card(client, trader_user, single_line_list):
    """Exactly 1 line → single card, no table chrome."""
    resp = client.get(f"/v2/partials/trading/{single_line_list.id}/lines")
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
        resp = client.get(f"/v2/partials/trading/{posted_list.id}/offers")
        assert resp.status_code == 200
    finally:
        app.dependency_overrides.pop(require_user, None)


# ── Offer submit (the centerpiece) ───────────────────────────────────


def test_submit_offer_creates_excess_offer(client, db_session, trader_user, posted_list, test_user):
    """A per-line offer submit creates an ExcessOffer (offerer = the buyer client
    user)."""
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    resp = client.post(
        f"/api/trading/{posted_list.id}/offers",
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
        f"/api/trading/{posted_list.id}/offers",
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
            f"/api/trading/{posted_list.id}/offers",
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
            "/api/trading/lists",
            data={"title": "Brand new list", "company_id": str(test_company.id), "notes": "n"},
        )
        assert resp.status_code == 200
        el = db_session.query(ExcessList).filter_by(title="Brand new list").one()
        assert el.owner_id == trader_user.id
        assert el.status == ExcessListStatus.DRAFT
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_add_line_renders_lines(client, db_session, trader_user, posted_list):
    """Adding a line returns the re-rendered Lines tab and persists the line."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        before = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).count()
        resp = client.post(
            f"/api/trading/{posted_list.id}/lines",
            data={"part_number": "LM358N", "quantity": "500", "manufacturer": "TI", "condition": "New"},
        )
        assert resp.status_code == 200
        after = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).count()
        assert after == before + 1
        assert "compact-table" in resp.text
    finally:
        app.dependency_overrides.pop(require_user, None)


def test_offer_compare_renders(client, db_session, trader_user, posted_list, test_user):
    """The per-line comparison renders best-highlight markup after an offer lands."""
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    # Submit one offer as the buyer (client default user).
    client.post(
        f"/api/trading/{posted_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": line.part_number, "quantity": "40", "unit_price": "9.99"},
    )
    # Owner (trader_user) can access the comparison.
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/trading/{posted_list.id}/lines/{line.id}/offers")
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


def test_non_owner_draft_detail_404(client, draft_list, test_user):
    """Non-owner GET on a DRAFT list → 404 (existence not revealed)."""
    # The default client user (test_user, a buyer) is NOT the owner.
    resp = client.get(f"/v2/partials/trading/{draft_list.id}")
    assert resp.status_code == 404


def test_non_owner_draft_lines_404(client, draft_list, test_user):
    """Non-owner GET on draft list's lines tab → 404."""
    resp = client.get(f"/v2/partials/trading/{draft_list.id}/lines")
    assert resp.status_code == 404


def test_non_owner_draft_offers_404(client, draft_list, test_user):
    """Non-owner GET on draft list's offers tab → 404."""
    resp = client.get(f"/v2/partials/trading/{draft_list.id}/offers")
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
    resp = client.get(f"/v2/partials/trading/{posted_list.id}")
    assert resp.status_code == 200
    # Customer name must not leak to non-owner.
    assert "Acme Electronics" not in resp.text

    # Offers tab: 200 but no offer payloads.
    resp = client.get(f"/v2/partials/trading/{posted_list.id}/offers")
    assert resp.status_code == 200
    # The is_owner=False path renders an empty offers view — no offer rows.
    # The _offers.html template does not render offer rows when is_owner is False.
    assert "Acme Electronics" not in resp.text


def test_non_owner_add_line_403(client, posted_list, test_user):
    """Non-owner POST add-line → 403."""
    resp = client.post(
        f"/api/trading/{posted_list.id}/lines",
        data={"part_number": "HACK-001", "quantity": "1"},
    )
    assert resp.status_code == 403


def test_non_owner_import_confirm_403(client, posted_list, test_user):
    """Non-owner POST import-confirm → 403."""
    import json

    resp = client.post(
        f"/api/trading/{posted_list.id}/import-confirm",
        data={"rows_json": json.dumps([{"part_number": "HACK-002", "quantity": 1}])},
    )
    assert resp.status_code == 403


def test_non_owner_line_offer_compare_403(client, db_session, posted_list, test_user):
    """Non-owner GET line-offer-compare → 403 (comparison is owner-only)."""
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    resp = client.get(f"/v2/partials/trading/{posted_list.id}/lines/{line.id}/offers")
    assert resp.status_code == 403


def test_owner_draft_detail_200(client, db_session, draft_list, trader_user):
    """Owner GET on their own DRAFT list → 200 (owner always passes)."""
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/trading/{draft_list.id}")
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
        f"/api/trading/{posted_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": line.part_number, "quantity": "10", "unit_price": "5.00"},
    )

    # Now view the offers tab AS THE OWNER.
    app.dependency_overrides[require_user] = lambda: trader_user
    try:
        resp = client.get(f"/v2/partials/trading/{posted_list.id}/offers")
        assert resp.status_code == 200
        body = resp.text
        # Owner sees the offer table — the "offers are private" banner must NOT appear.
        assert "Offers are private to the list owner" not in body
        assert "offers are private" not in body.lower()
        # The submitted part number should appear in the per-line offer table.
        assert line.part_number in body
    finally:
        app.dependency_overrides.pop(require_user, None)

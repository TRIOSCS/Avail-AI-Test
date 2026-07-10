"""test_resell_line_select.py — Per-line multi-select on the Resell Lines tab.

Covers the UX-audit gap: the offer-to-buyers panel already supports a per-line scope
(``?line_ids=…`` → ``scope=per_line`` + ``scope_lines``), but the Lines table had no way
to pick lines, so a trader could only ever offer the whole list. These tests pin the new
reachability:

  - the multi-line Lines table (owner + posted) renders per-line checkboxes + the
    "Offer selected lines" action bar wired to the offer-buyers panel via :hx-vals;
  - opening the panel with ``line_ids=[…]`` scopes it to exactly those lines
    (per_line scope + "These N lines" label + hidden line_ids field);
  - owner-gating is preserved — a non-owner sees no checkboxes and 403s on the panel;
  - a draft list (owner but not posted) shows no offer action (nothing to offer yet).

Exercised with the TestClient. Owner-path tests override require_user to the trader who
owns the seeded list (the endpoints are owner-gated).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.utils.normalization import normalize_mpn_key

_CAP = "capacitors"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def trader_user(db_session: Session) -> User:
    """The list owner — a trader (owns the list = the one who may offer it out)."""
    user = User(
        email="ls-trader@trioscs.com",
        name="Ls Trader",
        role="trader",
        azure_id="ls-azure-trader",
        m365_connected=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _add_lines(db: Session, el: ExcessList, part_numbers: list[str]) -> list[ExcessLineItem]:
    """Attach a set of capacitor lines to *el* and return them (id order)."""
    items = []
    for pn in part_numbers:
        item = ExcessLineItem(
            excess_list_id=el.id,
            part_number=pn,
            normalized_part_number=normalize_mpn_key(pn),
            quantity=1000,
            condition="New",
            asking_price=Decimal("1.00"),
        )
        db.add(item)
        items.append(item)
    db.flush()
    return items


@pytest.fixture()
def posted_multiline_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A posted (collecting) list owned by the trader with TWO lines → multi-line
    table."""
    el = ExcessList(
        title="Ls surplus caps",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=2,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    _add_lines(db_session, el, ["GRM188R", "C0402X"])
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def draft_multiline_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A DRAFT list owned by the trader with TWO lines (nothing to offer yet)."""
    el = ExcessList(
        title="Ls draft caps",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.DRAFT,
        total_line_items=2,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    _add_lines(db_session, el, ["GRM188R", "C0402X"])
    db_session.commit()
    db_session.refresh(el)
    return el


def _own(user: User):
    """Override require_user to *user* (the owner).

    Returns a cleanup callable.
    """
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user
    return lambda: app.dependency_overrides.pop(require_user, None)


def _line_ids(db: Session, el: ExcessList) -> list[int]:
    return [li.id for li in db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()]


# ── Lines table renders the selection UI (owner + posted) ────────────


def test_lines_table_renders_checkboxes_and_offer_action(client, db_session, trader_user, posted_multiline_list):
    """The owner's multi-line Lines table renders per-line checkboxes + the 'Offer
    selected lines' action wired to the offer-buyers panel via :hx-vals."""
    restore = _own(trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_multiline_list.id}/lines")
        assert resp.status_code == 200
        body = resp.text
        ids = _line_ids(db_session, posted_multiline_list)
        # Per-line checkboxes: a per-row toggle for each line + the select-all.
        for lid in ids:
            assert f"toggle({lid})" in body
            assert f"selectedLines.includes({lid})" in body
        assert "toggleAll($event)" in body
        # The action bar + its wiring to the existing per-line panel scope.
        assert "Offer selected lines" in body
        assert f"/v2/partials/resell/{posted_multiline_list.id}/offer-buyers-form" in body
        assert "line_ids: selectedLines.join" in body
    finally:
        restore()


def test_lines_table_hides_selection_for_non_owner(client, db_session, posted_multiline_list):
    """A non-owner (the default buyer client user) may view a posted list's lines but
    never sees the checkboxes or the offer action — offering out is the owner's
    action."""
    resp = client.get(f"/v2/partials/resell/{posted_multiline_list.id}/lines")
    assert resp.status_code == 200
    body = resp.text
    assert "Offer selected lines" not in body
    assert "toggleAll($event)" not in body


def test_lines_table_no_offer_action_on_draft(client, db_session, trader_user, draft_multiline_list):
    """A draft list (owner, but not posted) shows no offer action — lines lock on post,
    there is nothing to offer to buyers yet."""
    restore = _own(trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{draft_multiline_list.id}/lines")
        assert resp.status_code == 200
        assert "Offer selected lines" not in resp.text
    finally:
        restore()


# ── Panel scopes to the selected line_ids (the reused mechanism) ─────


def test_panel_scopes_to_selected_line_ids(client, db_session, trader_user, posted_multiline_list):
    """Opening the offer-buyers panel with ?line_ids=[…] scopes it to exactly those
    lines: per_line scope seed, the 'These N lines' label, and the hidden line_ids field."""
    ids = _line_ids(db_session, posted_multiline_list)
    restore = _own(trader_user)
    try:
        resp = client.get(
            f"/v2/partials/resell/{posted_multiline_list.id}/offer-buyers-form",
            params={"line_ids": ",".join(str(i) for i in ids)},
        )
        assert resp.status_code == 200
        body = resp.text
        # Scope defaults to per_line (line_ids present) and the label reflects the count.
        assert '"per_line"' in body
        assert f"These {len(ids)} lines" in body
        assert f"{len(ids)} selected lines" in body
        # The selected ids ride into the POST via the hidden field.
        assert f'name="line_ids" value="{",".join(str(i) for i in ids)}"' in body
    finally:
        restore()


def test_panel_scope_single_selected_line(client, db_session, trader_user, posted_multiline_list):
    """Selecting exactly one line scopes the panel to that single line (singular
    label)."""
    ids = _line_ids(db_session, posted_multiline_list)
    restore = _own(trader_user)
    try:
        resp = client.get(
            f"/v2/partials/resell/{posted_multiline_list.id}/offer-buyers-form",
            params={"line_ids": str(ids[0])},
        )
        assert resp.status_code == 200
        body = resp.text
        assert '"per_line"' in body
        assert "These 1 line" in body  # singular
        assert f'name="line_ids" value="{ids[0]}"' in body
    finally:
        restore()


def test_panel_line_ids_owner_gated(client, db_session, posted_multiline_list):
    """A non-owner cannot open the scoped panel → 403 (owner-only), even with
    line_ids."""
    ids = _line_ids(db_session, posted_multiline_list)
    resp = client.get(
        f"/v2/partials/resell/{posted_multiline_list.id}/offer-buyers-form",
        params={"line_ids": ",".join(str(i) for i in ids)},
    )
    assert resp.status_code == 403

"""test_resell_row_signal.py — Non-owner row content signal + owner-only Ask column
(Phase-3 resell Task 4, R4 + R5).

R4: the non-owner ("Open to Me") left-list row must read like "12 lines · Xilinx,
Texas Instruments · New" instead of a bare "Anonymized posting" — line count, top
distinct manufacturers (capped at 3), and a condition summary, computed in ONE grouped
query over ExcessLineItem per page of list ids. This is non-identifying: seller/customer
identity, offer coverage, and offer count stay hidden (D2 discipline, unchanged).

R5: ``ExcessLineItem.asking_price`` ("Ask") is owner-only — it renders in the Lines tab
(single-card + table shapes), the per-line offer-comparison modal, and the Build-Bid
table, but never leaks to a non-owner render of the same data.

Called by: pytest.
Depends on: conftest fixtures (client auths as test_user, a non-owner buyer),
    app.routers.resell, app.models.excess.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.dependencies import require_user
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.utils.normalization import normalize_mpn_key


@pytest.fixture()
def owner_user(db_session: Session) -> User:
    """The list owner — distinct from the default (non-owner) test_user client."""
    user = User(
        email="rowsignal-owner@trioscs.com",
        name="Rhea Owner",
        role="trader",
        azure_id="test-azure-rowsignal-owner",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _line(pn: str, manufacturer: str, condition: str = "New", *, asking_price=None, qty: int = 10) -> dict:
    return {
        "part_number": pn,
        "manufacturer": manufacturer,
        "condition": condition,
        "quantity": qty,
        "asking_price": asking_price,
    }


def _posted_list(db_session: Session, owner: User, company: Company, lines: list[dict]) -> ExcessList:
    el = ExcessList(
        title="Customer excess — do not leak this title",
        company_id=company.id,
        owner_id=owner.id,
        status=ExcessListStatus.COLLECTING,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    for spec in lines:
        db_session.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number=spec["part_number"],
                normalized_part_number=normalize_mpn_key(spec["part_number"]),
                manufacturer=spec["manufacturer"],
                condition=spec["condition"],
                quantity=spec["quantity"],
                asking_price=spec["asking_price"],
            )
        )
    db_session.commit()
    db_session.refresh(el)
    return el


def _as_owner(client, owner: User):
    client.app.dependency_overrides[require_user] = lambda: owner
    return client


def _clear_owner_override(client):
    client.app.dependency_overrides.pop(require_user, None)


# ── (a) R4: non-owner row shows the content signal, still no customer name ────


def test_non_owner_row_shows_line_manufacturer_condition_summary(
    client, db_session, owner_user, test_company, test_user
):
    """A non-owner ("open" lens) row reads like '2 lines · Xilinx, Texas Instruments ·
    New' — line count, both distinct manufacturers, and the shared condition — while the
    seller identity (company name, real title) stays hidden."""
    assert test_user.id != owner_user.id
    el = _posted_list(
        db_session,
        owner_user,
        test_company,
        [
            _line("XCVU9P-2FLGA2104I", "Xilinx"),
            _line("SN74LVC1G17DBVR", "Texas Instruments"),
        ],
    )

    body = client.get("/v2/partials/resell/lists?lens=open").text
    assert "2 lines" in body
    assert "Xilinx" in body
    assert "Texas Instruments" in body
    assert "New" in body
    # Seller identity stays hidden — same D2 discipline as before this task.
    assert test_company.name not in body
    assert el.title not in body
    assert f"Excess listing #{el.id}" in body


def test_non_owner_row_summary_caps_manufacturers_at_three(client, db_session, owner_user, test_company, test_user):
    """Cap 3: with four distinct manufacturers, only the top 3 by line count surface."""
    assert test_user.id != owner_user.id
    _posted_list(
        db_session,
        owner_user,
        test_company,
        [
            _line("PN-A1", "AlphaCorp"),
            _line("PN-A2", "AlphaCorp"),
            _line("PN-B1", "BetaCorp"),
            _line("PN-B2", "BetaCorp"),
            _line("PN-C1", "GammaCorp"),
            _line("PN-C2", "GammaCorp"),
            _line("PN-D1", "DeltaCorp"),  # lowest frequency — dropped by the cap
        ],
    )

    body = client.get("/v2/partials/resell/lists?lens=open").text
    assert "AlphaCorp" in body
    assert "BetaCorp" in body
    assert "GammaCorp" in body
    assert "DeltaCorp" not in body


def test_non_owner_row_signal_replaces_anonymized_posting_placeholder(
    client, db_session, owner_user, test_company, test_user
):
    """The old bare 'Anonymized posting' placeholder is gone once a non-owner row has
    line data to summarize."""
    assert test_user.id != owner_user.id
    _posted_list(db_session, owner_user, test_company, [_line("XCVU9P-2FLGA2104I", "Xilinx")])

    body = client.get("/v2/partials/resell/lists?lens=open").text
    assert "Anonymized posting" not in body


# ── (b) R5: owner Lines view shows Ask ─────────────────────────────────────


def test_owner_lines_table_view_shows_ask(client, db_session, owner_user, test_company, test_user):
    """Owner viewing the (≥2-line, table-shape) Lines tab sees the Ask price per
    line."""
    el = _posted_list(
        db_session,
        owner_user,
        test_company,
        [
            _line("XCVU9P-2FLGA2104I", "Xilinx", asking_price=Decimal("12.5000")),
            _line("SN74LVC1G17DBVR", "Texas Instruments", asking_price=Decimal("0.4200")),
        ],
    )

    _as_owner(client, owner_user)
    try:
        resp = client.get(f"/v2/partials/resell/{el.id}/lines")
    finally:
        _clear_owner_override(client)
    assert resp.status_code == 200
    assert "$12.5000" in resp.text
    assert "$0.4200" in resp.text


def test_owner_lines_single_card_view_shows_ask(client, db_session, owner_user, test_company, test_user):
    """Owner viewing the (1-line, card-shape) Lines tab sees the Ask price."""
    el = _posted_list(
        db_session,
        owner_user,
        test_company,
        [_line("XCVU9P-2FLGA2104I", "Xilinx", asking_price=Decimal("99.0000"))],
    )

    _as_owner(client, owner_user)
    try:
        resp = client.get(f"/v2/partials/resell/{el.id}/lines")
    finally:
        _clear_owner_override(client)
    assert resp.status_code == 200
    assert "$99.0000" in resp.text


def test_owner_offer_compare_shows_ask(client, db_session, owner_user, test_company, test_user):
    """Owner viewing the per-line offer-comparison modal sees the line's Ask price."""
    el = _posted_list(
        db_session,
        owner_user,
        test_company,
        [_line("XCVU9P-2FLGA2104I", "Xilinx", asking_price=Decimal("55.5500"))],
    )
    item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()

    _as_owner(client, owner_user)
    try:
        resp = client.get(f"/v2/partials/resell/{el.id}/lines/{item.id}/offers")
    finally:
        _clear_owner_override(client)
    assert resp.status_code == 200
    assert "$55.5500" in resp.text


def test_owner_build_bid_shows_ask(client, db_session, owner_user, test_company, test_user):
    """Owner viewing the Build-Bid table sees each line's Ask price."""
    el = _posted_list(
        db_session,
        owner_user,
        test_company,
        [_line("XCVU9P-2FLGA2104I", "Xilinx", asking_price=Decimal("7.2500"))],
    )

    _as_owner(client, owner_user)
    try:
        resp = client.get(f"/v2/partials/resell/{el.id}/build-bid")
    finally:
        _clear_owner_override(client)
    assert resp.status_code == 200
    assert "$7.2500" in resp.text


# ── (c) R5: non-owner render of the same data has NO asking-price value ────


def test_non_owner_lines_view_hides_ask(client, db_session, owner_user, test_company, test_user):
    """A non-owner viewing the same posted list's Lines tab never sees the Ask value
    (identity/oracle discipline — the price data never reaches the non-owner render)."""
    assert test_user.id != owner_user.id
    el = _posted_list(
        db_session,
        owner_user,
        test_company,
        [
            _line("XCVU9P-2FLGA2104I", "Xilinx", asking_price=Decimal("12.5000")),
            _line("SN74LVC1G17DBVR", "Texas Instruments", asking_price=Decimal("0.4200")),
        ],
    )

    resp = client.get(f"/v2/partials/resell/{el.id}/lines")
    assert resp.status_code == 200
    assert "12.5000" not in resp.text
    assert "0.4200" not in resp.text


def test_non_owner_single_card_lines_view_hides_ask(client, db_session, owner_user, test_company, test_user):
    """Same non-leak, single-card shape."""
    assert test_user.id != owner_user.id
    el = _posted_list(
        db_session,
        owner_user,
        test_company,
        [_line("XCVU9P-2FLGA2104I", "Xilinx", asking_price=Decimal("99.0000"))],
    )

    resp = client.get(f"/v2/partials/resell/{el.id}/lines")
    assert resp.status_code == 200
    assert "99.0000" not in resp.text


# ── (d) search placeholder updated ──────────────────────────────────────────


def test_search_placeholder_mentions_part_or_manufacturer(client):
    body = client.get("/v2/partials/resell/lists").text
    assert "Search by part or manufacturer…" in body
    assert "Search lists…" not in body

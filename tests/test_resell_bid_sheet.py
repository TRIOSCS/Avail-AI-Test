"""test_resell_bid_sheet.py — Blank Bid Sheet CSV export (Resell bid round trip, piece
1).

Covers GET /v2/partials/resell/{list_id}/bid-sheet: an owner-only CSV download of the
list's active (available/bidding) line items with blank bidder-fill columns (Bidder /
Offer Qty / Unit Price / Lead Time (Days) / Notes) so several bidders' filled-in copies
can be concatenated into one compiled sheet and re-uploaded via the bid-upload flow.

Called by: pytest
Depends on: app.routers.resell, app.models.excess, tests.conftest
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessLineItemStatus, ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.utils.normalization import normalize_mpn_key


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


@pytest.fixture()
def trader_user(db_session: Session) -> User:
    user = User(
        email="sheet-trader@trioscs.com",
        name="Sheet Trader",
        role="trader",
        azure_id="test-azure-sheet-trader",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def posted_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A posted list with a mix of line statuses
    (available/bidding/awarded/withdrawn)."""
    el = ExcessList(
        title="Bid sheet surplus",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=4,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    specs = [
        ("LM358N", ExcessLineItemStatus.AVAILABLE),
        ("NE555P", ExcessLineItemStatus.BIDDING),
        ("MAX232CPE", ExcessLineItemStatus.AWARDED),
        ("DS1307Z", ExcessLineItemStatus.WITHDRAWN),
    ]
    for mpn, status in specs:
        db_session.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number=mpn,
                normalized_part_number=normalize_mpn_key(mpn),
                manufacturer="Texas Instruments",
                description="Test part",
                quantity=100,
                condition="New",
                date_code="2024+",
                status=status,
            )
        )
    db_session.commit()
    db_session.refresh(el)
    return el


def _own(user: User):
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user
    return lambda: app.dependency_overrides.pop(require_user, None)


def test_bid_sheet_owner_200_with_active_rows_only(client, db_session, trader_user, posted_list):
    """Owner gets 200 CSV; one row per available/bidding line, awarded/withdrawn
    excluded."""
    restore = _own(trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/bid-sheet")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert f"resell_bid_sheet_list_{posted_list.id}.csv" in resp.headers["content-disposition"]

        rows = _parse_csv(resp.text)
        header = rows[0]
        assert header[:7] == [
            "Line ID",
            "Part Number",
            "Manufacturer",
            "Description",
            "Qty Available",
            "Condition",
            "Date Code",
        ]
        assert header[7:] == ["Bidder", "Offer Qty", "Unit Price", "Lead Time (Days)", "Notes"]

        data_rows = rows[1:]
        assert len(data_rows) == 2  # AVAILABLE + BIDDING only
        body_mpns = {r[1] for r in data_rows}
        assert body_mpns == {"LM358N", "NE555P"}
        # Bidder-fill columns are blank.
        for r in data_rows:
            assert r[7:] == ["", "", "", "", ""]
    finally:
        restore()


def test_bid_sheet_excludes_awarded_and_withdrawn(client, db_session, trader_user, posted_list):
    restore = _own(trader_user)
    try:
        rows = _parse_csv(client.get(f"/v2/partials/resell/{posted_list.id}/bid-sheet").text)
        body_mpns = {r[1] for r in rows[1:]}
        assert "MAX232CPE" not in body_mpns
        assert "DS1307Z" not in body_mpns
    finally:
        restore()


def test_bid_sheet_non_owner_403(client, posted_list, test_user):
    """Default client user (buyer) is not the owner → 403."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/bid-sheet")
    assert resp.status_code == 403


def test_bid_sheet_ordered_by_id(client, db_session, trader_user, posted_list):
    restore = _own(trader_user)
    try:
        rows = _parse_csv(client.get(f"/v2/partials/resell/{posted_list.id}/bid-sheet").text)
        ids = [int(r[0]) for r in rows[1:]]
        assert ids == sorted(ids)
    finally:
        restore()


def test_bid_sheet_formula_injection_safe(client, db_session, trader_user, test_company):
    """A part number starting with '=' arrives quoted (CSV formula-injection guard)."""
    el = ExcessList(
        title="Injection test",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.OPEN,
        total_line_items=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="=CMD('bad')",
            quantity=10,
            condition="New",
            status=ExcessLineItemStatus.AVAILABLE,
        )
    )
    db_session.commit()
    db_session.refresh(el)

    restore = _own(trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{el.id}/bid-sheet")
        assert resp.status_code == 200
        rows = _parse_csv(resp.text)
        assert rows[1][1] == "'=CMD('bad')"
    finally:
        restore()

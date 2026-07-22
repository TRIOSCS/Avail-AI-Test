"""test_resell_bid_csv.py — Build-Bid CSV download (Resell bid round trip, piece 3).

Covers GET /api/resell/{list_id}/bid/{bid_id}/csv: the spreadsheet twin of the clean
bid-back PDF, built ONLY from ``bid_back_service.bid_back_export_context`` (the
identity-clean whitelist) — never the inbound offer/rollup/vendor fields — plus a
trailing Total row carrying the subtotal.

Called by: pytest
Depends on: app.services.bid_back_service, app.models.excess, tests.conftest
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.services import bid_back_service
from app.utils.normalization import normalize_mpn_key


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


@pytest.fixture()
def trader_user(db_session: Session) -> User:
    user = User(
        email="csv-trader@trioscs.com",
        name="CSV Trader",
        role="trader",
        azure_id="test-azure-csv-trader",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def posted_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    el = ExcessList(
        title="CSV bid surplus",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=2,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    for mpn, mfr in (("LM358N", "Texas Instruments"), ("NE555P", "STMicro")):
        db_session.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number=mpn,
                normalized_part_number=normalize_mpn_key(mpn),
                manufacturer=mfr,
                quantity=200,
                condition="New",
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


def _assemble_bid(db_session, posted_list, trader_user):
    items = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).order_by(ExcessLineItem.id).all()
    selections = [
        {"excess_line_item_id": items[0].id, "customer_unit_price": "1.5000"},
        {"excess_line_item_id": items[1].id, "customer_unit_price": "2.0000"},
    ]
    return bid_back_service.build_bid_back(db_session, list_id=posted_list.id, owner=trader_user, selections=selections)


def test_bid_csv_owner_200_rows_match_lines_plus_total(client, db_session, trader_user, posted_list):
    bid = _assemble_bid(db_session, posted_list, trader_user)
    restore = _own(trader_user)
    try:
        resp = client.get(f"/api/resell/{posted_list.id}/bid/{bid.id}/csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert f"BID-{bid.id}.csv" in resp.headers["content-disposition"]

        rows = _parse_csv(resp.text)
        assert rows[0] == ["Part Number", "Manufacturer", "Condition", "Quantity", "Unit Price", "Extended Price"]
        assert len(rows) == 4  # header + 2 lines + Total
        assert rows[1][0] == "LM358N"
        assert rows[1][1] == "Texas Instruments"
        assert rows[1][3] == "200"
        assert rows[1][4] == "1.5000"  # money cells are FORMATTED (unit 4dp), never raw float reprs
        assert rows[1][5] == "300.00"  # extended 2dp — matches the PDF's {:,.2f}
        assert rows[2][0] == "NE555P"

        total_row = rows[-1]
        assert total_row[0] == "Total"
        expected_subtotal = float(Decimal("1.5000") * 200 + Decimal("2.0000") * 200)
        assert float(total_row[-1]) == pytest.approx(expected_subtotal)
        assert total_row[-1] == "700.00"
    finally:
        restore()


def test_bid_csv_money_cells_never_leak_float_artifacts(client, db_session, trader_user, posted_list):
    """A $0.07 × 3 line reads "0.21", never "0.21000000000000002" — and the Total row
    equals the sum of the printed line extendeds (canonical rounding lives in
    bid_back_export_context, shared by PDF and CSV)."""
    tiny = ExcessLineItem(
        excess_list_id=posted_list.id,
        part_number="TINY-QTY-3",
        normalized_part_number=normalize_mpn_key("TINY-QTY-3"),
        manufacturer="Acme",
        quantity=3,
        condition="New",
    )
    db_session.add(tiny)
    db_session.commit()
    bid = bid_back_service.build_bid_back(
        db_session,
        list_id=posted_list.id,
        owner=trader_user,
        selections=[{"excess_line_item_id": tiny.id, "customer_unit_price": "0.07"}],
    )
    restore = _own(trader_user)
    try:
        resp = client.get(f"/api/resell/{posted_list.id}/bid/{bid.id}/csv")
        assert resp.status_code == 200
        rows = _parse_csv(resp.text)
        line = rows[1]
        assert line[4] == "0.0700"
        assert line[5] == "0.21"
        assert rows[-1] == ["Total", "", "", "", "", "0.21"]
    finally:
        restore()


def test_bid_csv_no_broker_identity_in_body(client, db_session, trader_user, posted_list):
    """The CSV body carries no offerer/vendor/broker identity — pure line-item
    whitelist."""
    from app.constants import ExcessOfferScope, ExcessOfferStatus
    from app.models import VendorCard
    from app.models.excess import ExcessOffer, ExcessOfferLine

    card = VendorCard(normalized_name="secret broker", display_name="Secret Broker Inc", emails=[], phones=[])
    db_session.add(card)
    db_session.flush()
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    offer = ExcessOffer(
        excess_list_id=posted_list.id,
        submitted_by=trader_user.id,
        offerer_vendor_card_id=card.id,
        scope=ExcessOfferScope.PER_LINE,
        status=ExcessOfferStatus.OPEN,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw=line.part_number,
            quantity=100,
            unit_price=Decimal("9.0000"),
        )
    )
    db_session.commit()

    bid = _assemble_bid(db_session, posted_list, trader_user)
    restore = _own(trader_user)
    try:
        resp = client.get(f"/api/resell/{posted_list.id}/bid/{bid.id}/csv")
        assert resp.status_code == 200
        assert "Secret Broker Inc" not in resp.text
        assert "9.0000" not in resp.text  # the inbound offer's own price never leaks in
    finally:
        restore()


def test_bid_csv_non_owner_blocked(client, db_session, trader_user, posted_list, test_user):
    bid = _assemble_bid(db_session, posted_list, trader_user)
    resp = client.get(f"/api/resell/{posted_list.id}/bid/{bid.id}/csv")
    assert resp.status_code == 403


def test_bid_csv_missing_bid_404(client, db_session, trader_user, posted_list):
    restore = _own(trader_user)
    try:
        resp = client.get(f"/api/resell/{posted_list.id}/bid/999999/csv")
        assert resp.status_code == 404
    finally:
        restore()

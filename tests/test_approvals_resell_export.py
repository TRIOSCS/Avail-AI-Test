"""test_approvals_resell_export.py — CSV export for the Resell offers / outreach data
(UX-audit gap: a manager could not pull these for reporting).

The Approvals-hub half of this file died in W4.3: the per-tab console export
(GET /v2/partials/approvals/{tab}/export) was cut with the 3-tab merge, and
test_approvals_hub_tabs.py pins its death.

Covers the Resell GET export endpoints
(GET /v2/partials/resell/{list_id}/offers|outreach/export): the competing-broker
Offers tab + the Outreach tracker — same owner-only gate as the tabs. Each endpoint:
200 + text/csv + attachment header, a header row + one row per matching record,
owner scoping, auth parity, and the export anchors rendering with hx-boost="false".

Called by: pytest
Depends on: conftest (client, unauthenticated_client, db_session, test_user, test_company),
            app.models.* , app.constants.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ExcessListStatus,
    ExcessOfferStatus,
    ExcessOutreachStatus,
    OfferLineMatchStatus,
)
from app.models import Company, User, VendorCard
from app.models.excess import (
    ExcessLineItem,
    ExcessList,
    ExcessOffer,
    ExcessOfferLine,
    ExcessOutreach,
)


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def _assert_attachment(resp, *, filename_contains: str) -> None:
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition
    assert filename_contains in disposition


# ══════════════════════════ Approvals hub exports — GONE (W4.3) ══════════════════════════
# The per-tab Approvals CSV export (GET /v2/partials/approvals/{tab}/export) died in
# W4.3 with the 3-tab merge; test_approvals_hub_tabs.py pins the route's death
# (registry + live 404). Only the Resell exports remain below.


# ══════════════════════════ Resell fixtures ══════════════════════════


@pytest.fixture()
def trader_user(db_session: Session) -> User:
    """The list owner — a trader (can post + owns the list = can offer it out)."""
    user = User(
        email="x-trader@trioscs.com",
        name="Ex Trader",
        role="trader",
        azure_id=f"x-az-{uuid.uuid4().hex[:8]}",
        m365_connected=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def posted_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A posted (collecting) list owned by the trader, with one line."""
    el = ExcessList(
        title="X surplus caps",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.BIDDING,
        total_line_items=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="GRM188R",
            quantity=1000,
            condition="Used",
            asking_price=Decimal("1.00"),
        )
    )
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


def _buyer_card(db: Session, name: str) -> VendorCard:
    vc = VendorCard(normalized_name=name.lower(), display_name=name)
    db.add(vc)
    db.flush()
    return vc


# ══════════════════════════ Resell — offers export ══════════════════════════


def test_offers_export_is_csv_attachment_with_rows(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    buyer = _buyer_card(db_session, "Broker Alpha")
    offer = ExcessOffer(
        excess_list_id=posted_list.id,
        submitted_by=trader_user.id,
        offerer_vendor_card_id=buyer.id,
        scope="per_line",
        status=ExcessOfferStatus.OPEN,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw="GRM188R",
            quantity=250,
            unit_price=Decimal("0.9000"),
            match_status=OfferLineMatchStatus.MATCHED,
        )
    )
    db_session.commit()

    restore = _own(trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers/export")
        _assert_attachment(resp, filename_contains=f"resell_offers_list_{posted_list.id}.csv")
        rows = _parse_csv(resp.text)
        assert rows[0][0] == "Offer ID" and "Broker" in rows[0] and "Condition" in rows[0]
        assert len(rows) == 2  # header + one offer line
        body = "\n".join(",".join(r) for r in rows[1:])
        assert "Broker Alpha" in body  # buyer (broker) name — owner-only view
        assert "GRM188R" in body
        assert "250" in body  # qty
        assert "0.9000" in body  # unit price
        assert "Used" in body  # condition (from matched line item)
        assert "open" in body  # status
    finally:
        restore()


def test_offers_export_take_all_row(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    buyer = _buyer_card(db_session, "Broker Whole")
    db_session.add(
        ExcessOffer(
            excess_list_id=posted_list.id,
            submitted_by=trader_user.id,
            offerer_vendor_card_id=buyer.id,
            scope="take_all",
            take_all_total_price=Decimal("5000.00"),
            status=ExcessOfferStatus.OPEN,
        )
    )
    db_session.commit()

    restore = _own(trader_user)
    try:
        rows = _parse_csv(client.get(f"/v2/partials/resell/{posted_list.id}/offers/export").text)
        assert len(rows) == 2  # header + one take-all summary row
        body = "\n".join(",".join(r) for r in rows[1:])
        assert "Broker Whole" in body
        assert "take_all" in body
        assert "5000.00" in body  # lump take-all total
    finally:
        restore()


def test_offers_export_owner_gated(client: TestClient, db_session: Session, posted_list: ExcessList):
    """A non-owner (default buyer client) cannot export the private offers → 403."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/offers/export")
    assert resp.status_code == 403


def test_offers_tab_renders_export_anchor(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    restore = _own(trader_user)
    try:
        html = client.get(f"/v2/partials/resell/{posted_list.id}/offers").text
        assert "Export CSV" in html
        assert 'hx-boost="false"' in html
        assert f"/v2/partials/resell/{posted_list.id}/offers/export" in html
    finally:
        restore()


# ══════════════════════════ Resell — outreach export ══════════════════════════


def test_outreach_export_is_csv_attachment_with_rows(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    buyer = _buyer_card(db_session, "Reach Buyer")
    db_session.add(
        ExcessOutreach(
            excess_list_id=posted_list.id,
            target_vendor_card_id=buyer.id,
            submitted_by=trader_user.id,
            channel="phone",
            status=ExcessOutreachStatus.BID,
            sent_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    restore = _own(trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach/export")
        _assert_attachment(resp, filename_contains=f"resell_outreach_list_{posted_list.id}.csv")
        rows = _parse_csv(resp.text)
        assert rows[0] == ["Buyer", "Line", "Channel", "Sent By", "Status", "Sent At", "Last Activity", "Note"]
        assert len(rows) == 2  # header + one outreach touch
        body = "\n".join(",".join(r) for r in rows[1:])
        assert "Reach Buyer" in body
        assert "phone" in body  # channel
        assert "bid" in body  # status
        assert "Whole list" in body  # no per-line item
        assert "Ex Trader" in body  # sent-by
    finally:
        restore()


def test_outreach_export_owner_gated(client: TestClient, db_session: Session, posted_list: ExcessList):
    """The tracker is the owner's private board → a non-owner export gets 403."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach/export")
    assert resp.status_code == 403


def test_outreach_tab_renders_export_anchor(
    client: TestClient, db_session: Session, trader_user: User, posted_list: ExcessList
):
    restore = _own(trader_user)
    try:
        html = client.get(f"/v2/partials/resell/{posted_list.id}/outreach").text
        assert "Export CSV" in html
        assert 'hx-boost="false"' in html
        assert f"/v2/partials/resell/{posted_list.id}/outreach/export" in html
    finally:
        restore()

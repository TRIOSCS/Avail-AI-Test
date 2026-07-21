"""test_resell_bid_upload.py — Multi-bidder compiled bid-sheet upload (Resell bid round
trip, piece 2).

Covers the owner-ingest-on-behalf-of-external-bidders flow:
  - ``excess_service.preview_bid_upload`` (read-only classification: line_id_match /
    mpn_match(matched/unmatched/ambiguous) / rejected — never silently coerced).
  - ``excess_service.upload_bids`` (ingestion): groups accepted rows by bidder, resolves/
    creates one VendorCard per bidder (reused, never duplicated), creates one
    ExcessOffer(scope=PER_LINE) + ExcessOfferLine per row per bidder, recomputes the
    best-price rollup for matched lines, never drops unmatched/ambiguous rows.
  - The upload-preview / upload-confirm ROUTES: owner-only, corrupt-file 400, non-owner
    403, and the full round trip via the TestClient.

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, app.models.vendors,
            tests.conftest
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOfferScope, OfferLineMatchStatus
from app.models import Company, User, VendorCard
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer
from app.services import excess_service
from app.utils.normalization import normalize_mpn_key


@pytest.fixture()
def trader_user(db_session: Session) -> User:
    user = User(
        email="upload-trader@trioscs.com",
        name="Upload Trader",
        role="trader",
        azure_id="test-azure-upload-trader",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def posted_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    el = ExcessList(
        title="Upload surplus",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=2,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    for mpn in ("LM358N", "NE555P"):
        db_session.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number=mpn,
                normalized_part_number=normalize_mpn_key(mpn),
                quantity=500,
                condition="New",
            )
        )
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def other_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A SECOND list, so a Line ID belonging to it is invalid on ``posted_list``."""
    el = ExcessList(
        title="Other list",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="OTHERLIST-1",
            normalized_part_number=normalize_mpn_key("OTHERLIST-1"),
            quantity=10,
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


def _row(bidder="Broker A", part_number="LM358N", quantity=100, unit_price="1.5000", **kw):
    row = {
        "bidder": bidder,
        "part_number": part_number,
        "quantity": quantity,
        "unit_price": unit_price,
        "lead_time_days": None,
        "notes": None,
        "line_id": None,
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# preview_bid_upload — read-only classification
# ---------------------------------------------------------------------------


def test_preview_mpn_matched_unmatched_ambiguous(db_session: Session, posted_list: ExcessList):
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id, part_number="LM358N").one()
    # A second line sharing the SAME normalized MPN makes it ambiguous.
    db_session.add(
        ExcessLineItem(
            excess_list_id=posted_list.id,
            part_number="LM358N",
            normalized_part_number=normalize_mpn_key("LM358N"),
            quantity=50,
            condition="New",
        )
    )
    db_session.commit()

    rows = [
        _row(bidder="Broker A", part_number="LM358N"),  # ambiguous (2 lines share this MPN)
        _row(bidder="Broker A", part_number="NE555P"),  # matched (exactly one line)
        _row(bidder="Broker A", part_number="DOES-NOT-EXIST"),  # unmatched
    ]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["rejected_count"] == 0
    assert result["accepted_count"] == 3
    statuses = {a["mpn_raw"]: a["match_status"] for a in result["accepted"]}
    assert statuses["LM358N"] == OfferLineMatchStatus.AMBIGUOUS
    assert statuses["NE555P"] == OfferLineMatchStatus.MATCHED
    assert statuses["DOES-NOT-EXIST"] == OfferLineMatchStatus.UNMATCHED
    assert line.id  # sanity: fixture line still exists


def test_preview_line_id_wins_over_mpn(db_session: Session, posted_list: ExcessList):
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id, part_number="NE555P").one()
    # Part Number text disagrees with the Line ID's real part — Line ID must win.
    rows = [_row(bidder="Broker A", part_number="MISMATCHED-TEXT", line_id=line.id)]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 1
    accepted = result["accepted"][0]
    assert accepted["classification"] == "line_id_match"
    assert accepted["match_status"] == OfferLineMatchStatus.MATCHED
    assert accepted["excess_line_item_id"] == line.id


def test_preview_invalid_line_id_falls_back_to_mpn(
    db_session: Session, posted_list: ExcessList, other_list: ExcessList
):
    """A Line ID belonging to ANOTHER list is invalid here — falls back to the MPN
    match."""
    foreign_line = db_session.query(ExcessLineItem).filter_by(excess_list_id=other_list.id).one()
    rows = [_row(bidder="Broker A", part_number="NE555P", line_id=foreign_line.id)]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 1
    accepted = result["accepted"][0]
    # Falls back to the MPN match path (never trusts a foreign Line ID), and NE555P
    # resolves cleanly on this list.
    assert accepted["classification"] == "mpn_match"
    assert accepted["match_status"] == OfferLineMatchStatus.MATCHED
    own_line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id, part_number="NE555P").one()
    assert accepted["excess_line_item_id"] == own_line.id


def test_preview_invalid_line_id_no_part_number_rejected(
    db_session: Session, posted_list: ExcessList, other_list: ExcessList
):
    """Invalid (foreign-list) Line ID + NO part number: an invalid Line ID contributes
    nothing usable, so this collapses to the SAME "nothing to identify the part" shape
    as a genuinely absent Line ID — rejected, same as the take-all-shape rule, never
    queued with a made-up identifier."""
    foreign_line = db_session.query(ExcessLineItem).filter_by(excess_list_id=other_list.id).one()
    rows = [_row(bidder="Broker A", part_number="", line_id=foreign_line.id)]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 1
    reason = result["rejected"][0]["reason"].lower()
    assert "line id" in reason or "part number" in reason


def test_preview_take_all_shape_rejected(db_session: Session, posted_list: ExcessList):
    """No Line ID and no Part Number → out of scope, rejected with a reason."""
    rows = [_row(bidder="Broker A", part_number="", line_id=None)]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 1
    assert (
        "line id" in result["rejected"][0]["reason"].lower() or "part number" in result["rejected"][0]["reason"].lower()
    )


@pytest.mark.parametrize("bad_qty", [0, -5, None, "", "abc"])
def test_preview_bad_quantity_rejected_never_coerced(db_session: Session, posted_list: ExcessList, bad_qty):
    rows = [_row(bidder="Broker A", part_number="LM358N", quantity=bad_qty)]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 1
    assert "quantity" in result["rejected"][0]["reason"].lower()


def test_preview_missing_bidder_rejected(db_session: Session, posted_list: ExcessList):
    rows = [_row(bidder="", part_number="LM358N")]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 1
    assert "bidder" in result["rejected"][0]["reason"].lower()


# ---------------------------------------------------------------------------
# upload_bids — ingestion into ExcessOffer / ExcessOfferLine
# ---------------------------------------------------------------------------


def test_upload_bids_one_offer_per_bidder(db_session: Session, trader_user: User, posted_list: ExcessList):
    rows = [
        _row(bidder="Broker A", part_number="LM358N", quantity=200, unit_price="1.1000"),
        _row(bidder="Broker A", part_number="NE555P", quantity=300, unit_price="0.9000"),
        _row(bidder="Broker B", part_number="LM358N", quantity=100, unit_price="1.3000"),
    ]
    result = excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert result["offers_created"] == 2
    assert result["lines_created"] == 3
    assert result["unmatched"] == 0
    assert result["rejected"] == 0

    offers = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).all()
    assert len(offers) == 2
    for offer in offers:
        assert offer.scope == ExcessOfferScope.PER_LINE
        assert offer.submitted_by == trader_user.id
        assert offer.notes == "Uploaded bid sheet"
    broker_a = next(o for o in offers if o.offerer_vendor_card.display_name == "Broker A")
    assert len(broker_a.lines) == 2
    broker_b = next(o for o in offers if o.offerer_vendor_card.display_name == "Broker B")
    assert len(broker_b.lines) == 1
    assert broker_b.lines[0].unit_price == Decimal("1.3000")


def test_upload_bids_reuses_existing_vendor_card(db_session: Session, trader_user: User, posted_list: ExcessList):
    from app.vendor_utils import normalize_vendor_name

    existing = VendorCard(
        normalized_name=normalize_vendor_name("Existing Broker"),
        display_name="Existing Broker",
        emails=[],
        phones=[],
        source="manual",
    )
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    rows = [_row(bidder="Existing Broker", part_number="LM358N")]
    excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)

    cards = db_session.query(VendorCard).filter(VendorCard.normalized_name == existing.normalized_name).all()
    assert len(cards) == 1  # no duplicate created
    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    assert offer.offerer_vendor_card_id == existing.id


def test_upload_bids_new_bidder_creates_card_with_source(
    db_session: Session, trader_user: User, posted_list: ExcessList
):
    rows = [_row(bidder="Brand New Broker", part_number="LM358N")]
    excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)

    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    card = offer.offerer_vendor_card
    assert card is not None
    assert card.display_name == "Brand New Broker"
    assert card.source == "resell_bid_upload"


def test_upload_bids_unmatched_ambiguous_queued_never_dropped(
    db_session: Session, trader_user: User, posted_list: ExcessList
):
    rows = [
        _row(bidder="Broker A", part_number="NOT-ON-THIS-LIST"),  # unmatched
        _row(bidder="Broker A", part_number=""),  # no identifier — rejected
    ]
    result = excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert result["offers_created"] == 1
    assert result["lines_created"] == 1  # the unmatched row IS created (queued)
    assert result["unmatched"] == 1
    assert result["rejected"] == 1  # the no-identifier row is dropped from creation

    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    line = offer.lines[0]
    assert line.match_status == OfferLineMatchStatus.UNMATCHED
    assert line.excess_line_item_id is None
    assert line.mpn_raw == "NOT-ON-THIS-LIST"


def test_upload_bids_recomputes_rollup(db_session: Session, trader_user: User, posted_list: ExcessList):
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id, part_number="LM358N").one()
    assert line.offer_count == 0

    rows = [
        _row(bidder="Broker A", part_number="LM358N", unit_price="1.0000"),
        _row(bidder="Broker B", part_number="LM358N", unit_price="2.5000"),
    ]
    excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    db_session.refresh(line)
    assert line.offer_count == 2
    assert line.best_offer_unit_price == Decimal("2.5000")


def test_upload_bids_non_owner_403(db_session: Session, test_user: User, posted_list: ExcessList):
    rows = [_row(bidder="Broker A", part_number="LM358N")]
    with pytest.raises(HTTPException) as exc_info:
        excess_service.upload_bids(db_session, list_id=posted_list.id, user=test_user, rows=rows)
    assert exc_info.value.status_code == 403


def test_upload_bids_empty_rows_400(db_session: Session, trader_user: User, posted_list: ExcessList):
    with pytest.raises(HTTPException) as exc_info:
        excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=[])
    assert exc_info.value.status_code == 400


def test_upload_bids_all_rejected_400(db_session: Session, trader_user: User, posted_list: ExcessList):
    rows = [_row(bidder="", part_number="LM358N")]
    with pytest.raises(HTTPException) as exc_info:
        excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert exc_info.value.status_code == 400


def test_upload_bids_draft_list_owner_gets_fix_it_400(db_session: Session, trader_user: User, posted_list: ExcessList):
    """The OWNER uploading on a still-draft list gets an honest 400, not a camouflage
    404 — the hide-the-draft 404 discipline exists for non-owners only (the 403 above
    already screens them out of this branch)."""
    posted_list.status = ExcessListStatus.DRAFT
    db_session.commit()
    rows = [_row(bidder="Broker A", part_number="LM358N")]
    with pytest.raises(HTTPException) as exc_info:
        excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert exc_info.value.status_code == 400
    assert "post the list" in str(exc_info.value.detail).lower()


# ---------------------------------------------------------------------------
# Router: upload-preview / upload-confirm
# ---------------------------------------------------------------------------


def test_upload_preview_draft_list_owner_gets_fix_it_400(client, db_session, trader_user, posted_list):
    restore = _own(trader_user)
    try:
        posted_list.status = ExcessListStatus.DRAFT
        db_session.commit()
        resp = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-preview",
            files={"file": ("sheet.csv", b"Bidder,Part Number,Offer Qty\nA,LM358N,10\n", "text/csv")},
        )
        assert resp.status_code == 400
        assert "post the list" in resp.json()["error"].lower()
    finally:
        restore()


def test_upload_preview_owner_only_403(client, posted_list, test_user):
    resp = client.post(
        f"/api/resell/{posted_list.id}/bids/upload-preview",
        files={"file": ("sheet.csv", b"bidder,part_number,quantity\nA,LM358N,10\n", "text/csv")},
    )
    assert resp.status_code == 403


def test_upload_preview_corrupt_file_distinct_error(client, db_session, trader_user, posted_list):
    restore = _own(trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-preview",
            files={"file": ("broken.xlsx", b"not a real xlsx", "application/vnd.ms-excel")},
        )
        assert resp.status_code == 400
        msg = resp.json()["error"].lower()
        assert "couldn't read" in msg or "could not read" in msg
    finally:
        restore()


def test_upload_preview_and_confirm_round_trip(client, db_session, trader_user, posted_list):
    restore = _own(trader_user)
    try:
        csv_bytes = (
            b"Bidder,Part Number,Offer Qty,Unit Price,Lead Time (Days),Notes\n"
            b"Broker A,LM358N,150,1.2000,10,fast ship\n"
            b"Broker B,NE555P,50,0.8000,,\n"
        )
        preview = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-preview",
            files={"file": ("bids.csv", csv_bytes, "text/csv")},
        )
        assert preview.status_code == 200
        assert "Broker A" in preview.text
        assert "Broker B" in preview.text

        # Extract the carried-forward rows_json hidden field value from the response.
        import re

        m = re.search(r'name="rows_json" value="(.*?)"', preview.text, re.DOTALL)
        assert m, "expected a rows_json hidden field in the preview"
        from html import unescape

        rows_json = unescape(m.group(1))

        confirm = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-confirm",
            data={"rows_json": rows_json},
        )
        assert confirm.status_code == 200
        trigger = confirm.headers.get("HX-Trigger")
        assert trigger
        payload = json.loads(trigger)
        assert "showToast" in payload
        assert "2" in payload["showToast"]["message"]  # 2 bids uploaded

        offers = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).all()
        assert len(offers) == 2
    finally:
        restore()


def test_upload_confirm_non_owner_403(client, posted_list, test_user):
    resp = client.post(
        f"/api/resell/{posted_list.id}/bids/upload-confirm",
        data={"rows_json": json.dumps([{"bidder": "X", "part_number": "LM358N", "quantity": 1}])},
    )
    assert resp.status_code == 403

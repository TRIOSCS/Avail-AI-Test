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


@pytest.mark.parametrize("unusable", ["Inc.", "LLC", "---"])
def test_preview_bidder_normalizing_to_empty_rejected(db_session: Session, posted_list: ExcessList, unusable):
    """A NON-blank bidder whose name normalizes to nothing (suffix-only, punctuation-
    only) is rejected per-row — it must never survive classification only to 422 the
    whole ingest inside resolve_bidder_card."""
    rows = [_row(bidder=unusable, part_number="LM358N")]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 1
    assert "bidder" in result["rejected"][0]["reason"].lower()


def test_preview_row_numbers_are_file_rows(db_session: Session, posted_list: ExcessList):
    """Rejected-row pointers use FILE row numbers — the header occupies row 1, so the
    first data row reports as row 2 (matches file_utils.extract_mpns_with_rows) and the
    owner's Excel cursor lands on the right line."""
    rows = [
        _row(bidder="", part_number="LM358N"),  # file row 2 — rejected
        _row(bidder="Broker A", part_number="NE555P"),  # file row 3 — accepted
        _row(bidder="Broker A", part_number="", line_id=None),  # file row 4 — rejected
    ]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert [r["row"] for r in result["rejected"]] == [2, 4]
    assert result["accepted"][0]["row"] == 3


def test_preview_non_string_cells_classify_without_raising(db_session: Session, posted_list: ExcessList):
    """Numeric/None cells in text columns (a tampered payload, or a spreadsheet numeric
    cell) are str()-coerced and classified like any other value — never an
    AttributeError."""
    rows = [
        {
            "bidder": 5,
            "part_number": 123,
            "quantity": 10,
            "unit_price": None,
            "lead_time_days": None,
            "notes": 7,
            "line_id": None,
        }
    ]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 1
    accepted = result["accepted"][0]
    assert accepted["bidder"] == "5"
    assert accepted["mpn_raw"] == "123"
    assert accepted["terms_text"] == "7"


def test_preview_non_dict_rows_rejected_with_reason(db_session: Session, posted_list: ExcessList):
    """Non-dict list elements classify as rejected rows (with a reason) — never
    raise."""
    result = excess_service.preview_bid_upload(db_session, posted_list.id, ["x", 1])  # type: ignore[list-item]
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 2
    assert all("malformed" in r["reason"].lower() for r in result["rejected"])


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


def test_upload_bids_open_list_flips_to_collecting(db_session: Session, trader_user: User, posted_list: ExcessList):
    """The FIRST ingested offer on an OPEN list flips it to COLLECTING (mirrors
    submit_offer)."""
    posted_list.status = ExcessListStatus.OPEN
    db_session.commit()
    rows = [_row(bidder="Broker A", part_number="LM358N")]
    excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    db_session.refresh(posted_list)
    assert posted_list.status == ExcessListStatus.COLLECTING


@pytest.mark.parametrize("status", [ExcessListStatus.BID_OUT, ExcessListStatus.AWARDED])
def test_upload_bids_late_statuses_untouched(db_session: Session, trader_user: User, posted_list: ExcessList, status):
    """A late upload on a bid_out/awarded list never rewrites the list status."""
    posted_list.status = status
    db_session.commit()
    rows = [_row(bidder="Broker A", part_number="LM358N")]
    excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    db_session.refresh(posted_list)
    assert posted_list.status == status


def test_upload_bids_past_close_at_stamps_late_even_though_status_still_collecting(
    db_session: Session, trader_user: User, posted_list: ExcessList
):
    """Finding #10: a compiled bid sheet uploaded after ``close_at`` lapsed — but before
    the nightly sweep flips the status — is stamped ``late``, not an indistinguishable
    on-time ``open``."""
    from datetime import timedelta

    posted_list.close_at = datetime.now(UTC) - timedelta(hours=3)
    db_session.commit()
    assert posted_list.status == ExcessListStatus.COLLECTING  # the nightly sweep hasn't run

    rows = [_row(bidder="Broker A", part_number="LM358N")]
    excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)

    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    assert offer.status == "late"


def test_upload_bids_case_variant_bidders_grouped_one_offer(
    db_session: Session, trader_user: User, posted_list: ExcessList
):
    """'Broker A' and 'BROKER A' are ONE bidder: grouped on the normalize_vendor_name
    key (the same key cards resolve on), so one bidder never yields two offers pointing
    at one VendorCard."""
    from app.vendor_utils import normalize_vendor_name

    rows = [
        _row(bidder="Broker A", part_number="LM358N"),
        _row(bidder="BROKER A", part_number="NE555P"),
    ]
    result = excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert result["offers_created"] == 1
    assert result["lines_created"] == 2

    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    assert len(offer.lines) == 2
    assert offer.offerer_vendor_card.display_name == "Broker A"  # first-seen spelling
    cards = db_session.query(VendorCard).filter(VendorCard.normalized_name == normalize_vendor_name("Broker A")).all()
    assert len(cards) == 1


def test_upload_bids_unusable_bidder_rejected_per_row_never_aborts(
    db_session: Session, trader_user: User, posted_list: ExcessList
):
    """A bidder name that normalizes to nothing ('Inc.') rejects THAT row only — the
    other bidders' offers still ingest (no mid-loop 422 aborting the whole sheet)."""
    rows = [
        _row(bidder="Inc.", part_number="LM358N"),
        _row(bidder="Broker A", part_number="NE555P"),
    ]
    result = excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert result["offers_created"] == 1
    assert result["lines_created"] == 1
    assert result["rejected"] == 1
    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    assert offer.offerer_vendor_card.display_name == "Broker A"


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


@pytest.mark.parametrize(
    "bad_payload",
    [
        "not json",  # unparsable
        "{}",  # parses, but not a list
        '"a string"',  # parses, but not a list
        "[1]",  # list, but element is not a dict
        '["x"]',  # list, but element is not a dict
        '[{"bidder": "A", "part_number": "LM358N", "quantity": 1}, 7]',  # one bad element poisons the payload
    ],
)
def test_upload_confirm_malformed_rows_json_400_never_500(client, db_session, trader_user, posted_list, bad_payload):
    """Every malformed rows_json shape degrades to the SAME clean 400 — never an
    AttributeError 500 (the confirm path must never trust the client payload)."""
    restore = _own(trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-confirm",
            data={"rows_json": bad_payload},
        )
        assert resp.status_code == 400
        assert "invalid bid upload payload" in resp.json()["error"].lower()
    finally:
        restore()


def test_upload_confirm_reclassifies_tampered_payload_server_side(
    client, db_session, trader_user, posted_list, other_list
):
    """L3 tamper-resistance at the CONFIRM boundary: a carried payload smuggling a
    foreign-list line_id plus fabricated match_status/excess_line_item_id fields is
    re-classified fresh — the write path uses the server's own MPN fallback, never the
    client's claimed match."""
    foreign_line = db_session.query(ExcessLineItem).filter_by(excess_list_id=other_list.id).one()
    own_line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id, part_number="NE555P").one()
    tampered = [
        {
            # Foreign line_id + fabricated match fields → MPN fallback resolves NE555P
            # on THIS list, never the foreign id the client claimed.
            "bidder": "Tamper Broker",
            "part_number": "NE555P",
            "quantity": 5,
            "unit_price": "1.0",
            "lead_time_days": None,
            "notes": None,
            "line_id": foreign_line.id,
            "match_status": "matched",
            "excess_line_item_id": foreign_line.id,
        },
        {
            # Nothing resolves on this list → unmatched, despite the claimed match.
            "bidder": "Tamper Broker",
            "part_number": "NOT-ON-THIS-LIST",
            "quantity": 5,
            "unit_price": None,
            "lead_time_days": None,
            "notes": None,
            "line_id": foreign_line.id,
            "match_status": "matched",
            "excess_line_item_id": foreign_line.id,
        },
    ]
    restore = _own(trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-confirm",
            data={"rows_json": json.dumps(tampered)},
        )
        assert resp.status_code == 200
        offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
        lines = sorted(offer.lines, key=lambda x: x.id)
        assert lines[0].excess_line_item_id == own_line.id  # server-side MPN fallback won
        assert lines[0].match_status == OfferLineMatchStatus.MATCHED
        assert lines[1].excess_line_item_id is None  # fabricated id ignored
        assert lines[1].match_status == OfferLineMatchStatus.UNMATCHED
    finally:
        restore()


def test_upload_confirm_oob_refreshes_lines_and_chips(client, db_session, trader_user, posted_list):
    """The confirm response is the _award_response OOB compose: Offers tab body PLUS
    out-of-band Lines tab and header chips — the ingest recomputes rollups and can flip
    the list status, so an Offers-only swap would leave those stale."""
    posted_list.status = ExcessListStatus.OPEN
    db_session.commit()
    rows = [_row(bidder="Broker A", part_number="LM358N")]
    restore = _own(trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-confirm",
            data={"rows_json": json.dumps(rows)},
        )
        assert resp.status_code == 200
        assert f'id="tab-lines-{posted_list.id}"' in resp.text
        assert f'id="resell-chips-{posted_list.id}"' in resp.text
        assert "hx-swap-oob" in resp.text
        db_session.expire_all()
        refreshed = db_session.get(ExcessList, posted_list.id)
        assert refreshed.status == ExcessListStatus.COLLECTING  # the flip is now visible in the chips render
    finally:
        restore()


# ---------------------------------------------------------------------------
# Finding #1 (P2) — a non-blank but unparseable/negative unit price REJECTS the row,
# rather than silently ingesting it with unit_price=None.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_price", ["TBD", "1.2.5", "-4.00"])
def test_preview_invalid_unit_price_rejected_never_nulled(db_session: Session, posted_list: ExcessList, bad_price):
    rows = [_row(bidder="Broker A", part_number="LM358N", unit_price=bad_price)]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 0
    assert result["rejected_count"] == 1
    assert "unit price" in result["rejected"][0]["reason"].lower()


@pytest.mark.parametrize("blank_price", [None, "", "   "])
def test_preview_blank_unit_price_accepted_with_none(db_session: Session, posted_list: ExcessList, blank_price):
    """Blank price stays optional and pins the existing behavior: the row is accepted
    with unit_price=None."""
    rows = [_row(bidder="Broker A", part_number="LM358N", unit_price=blank_price)]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, rows)
    assert result["accepted_count"] == 1
    assert result["rejected_count"] == 0
    assert result["accepted"][0]["unit_price"] is None


def test_upload_bids_invalid_unit_price_rejected(db_session: Session, trader_user: User, posted_list: ExcessList):
    """A row with an unparseable price is dropped from ingestion entirely (never
    ingested with unit_price=None) — with only that one bad row, nothing is left to
    upload."""
    rows = [_row(bidder="Broker A", part_number="LM358N", unit_price="call")]
    with pytest.raises(HTTPException) as exc_info:
        excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert exc_info.value.status_code == 400


def test_upload_bids_invalid_unit_price_rejected_alongside_valid_row(
    db_session: Session, trader_user: User, posted_list: ExcessList
):
    """A bad-price row is rejected while a sibling valid row from another bidder still
    ingests normally — the rejection is per-row, never a whole-sheet abort."""
    rows = [
        _row(bidder="Broker A", part_number="LM358N", unit_price="call"),
        _row(bidder="Broker B", part_number="NE555P", unit_price="0.9000"),
    ]
    result = excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert result["offers_created"] == 1
    assert result["rejected"] == 1
    assert result["lines_created"] == 1
    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    assert offer.offerer_vendor_card.display_name == "Broker B"


# ---------------------------------------------------------------------------
# Finding #2 (P2) — re-uploading a corrected sheet SUPERSEDES (withdraw + replace) an
# earlier uploaded offer from the same bidder, instead of duplicating it.
# ---------------------------------------------------------------------------


def test_upload_bids_reupload_supersedes_old_offer(db_session: Session, trader_user: User, posted_list: ExcessList):
    from app.constants import ExcessOfferStatus

    rows = [_row(bidder="Broker A", part_number="LM358N", unit_price="1.0000")]
    first = excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert first["offers_created"] == 1
    assert first["superseded"] == 0

    old_offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    old_offer_id = old_offer.id

    rows2 = [_row(bidder="Broker A", part_number="LM358N", unit_price="1.2000")]
    second = excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows2)
    assert second["offers_created"] == 1
    assert second["superseded"] == 1

    offers = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).all()
    assert len(offers) == 2  # the withdrawn original PLUS the replacement — never deleted
    old = db_session.get(ExcessOffer, old_offer_id)
    assert old.status == ExcessOfferStatus.WITHDRAWN

    open_offers = [o for o in offers if o.status == ExcessOfferStatus.OPEN]
    assert len(open_offers) == 1  # exactly one open offer per bidder after the re-upload
    assert open_offers[0].id != old_offer_id

    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id, part_number="LM358N").one()
    assert line.offer_count == 1  # offer_count back to 1, not 2
    assert line.best_offer_unit_price == Decimal("1.2000")


def test_upload_bids_reupload_leaves_manual_offer_untouched(
    db_session: Session, trader_user: User, posted_list: ExcessList
):
    """A MANUAL offer (different ``notes``) from the same VendorCard is never superseded
    by an upload — only earlier UPLOADED offers are supersede-eligible."""
    from app.constants import ExcessOfferStatus
    from app.vendor_utils import normalize_vendor_name

    card = VendorCard(
        normalized_name=normalize_vendor_name("Broker A"),
        display_name="Broker A",
        emails=[],
        phones=[],
        source="manual",
    )
    db_session.add(card)
    db_session.flush()
    manual_offer = ExcessOffer(
        excess_list_id=posted_list.id,
        submitted_by=trader_user.id,
        offerer_vendor_card_id=card.id,
        scope=ExcessOfferScope.PER_LINE,
        status=ExcessOfferStatus.OPEN,
        notes="Submitted via broker portal",
    )
    db_session.add(manual_offer)
    db_session.commit()
    db_session.refresh(manual_offer)

    rows = [_row(bidder="Broker A", part_number="LM358N")]
    result = excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    assert result["superseded"] == 0

    db_session.refresh(manual_offer)
    assert manual_offer.status == ExcessOfferStatus.OPEN


def test_upload_bids_reupload_never_withdraws_won_offer(
    db_session: Session, trader_user: User, posted_list: ExcessList
):
    """A WON uploaded offer is a resolved sale, not "in play" — a re-upload for the same
    bidder must never touch it."""
    from app.constants import ExcessOfferStatus

    rows = [_row(bidder="Broker A", part_number="LM358N", unit_price="1.0000")]
    excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)
    won_offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).one()
    won_offer.status = ExcessOfferStatus.WON
    db_session.commit()

    rows2 = [_row(bidder="Broker A", part_number="LM358N", unit_price="1.5000")]
    result = excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows2)
    assert result["superseded"] == 0

    db_session.refresh(won_offer)
    assert won_offer.status == ExcessOfferStatus.WON
    offers = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).all()
    assert len(offers) == 2


def test_preview_flags_bidder_with_existing_upload(db_session: Session, trader_user: User, posted_list: ExcessList):
    rows = [_row(bidder="Broker A", part_number="LM358N")]
    excess_service.upload_bids(db_session, list_id=posted_list.id, user=trader_user, rows=rows)

    preview_rows = [_row(bidder="Broker A", part_number="LM358N", unit_price="1.9000")]
    result = excess_service.preview_bid_upload(db_session, posted_list.id, preview_rows)
    assert result["supersedes_by_bidder"].get("Broker A") is True


def test_preview_no_flag_for_bidder_without_existing_upload(db_session: Session, posted_list: ExcessList):
    result = excess_service.preview_bid_upload(
        db_session, posted_list.id, [_row(bidder="Broker A", part_number="LM358N")]
    )
    assert result["supersedes_by_bidder"].get("Broker A") is False


def test_upload_confirm_toast_includes_superseded_count(client, db_session, trader_user, posted_list):
    restore = _own(trader_user)
    try:
        client.post(
            f"/api/resell/{posted_list.id}/bids/upload-confirm",
            data={"rows_json": json.dumps([_row(bidder="Broker A", part_number="LM358N", unit_price="1.0000")])},
        )
        resp = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-confirm",
            data={"rows_json": json.dumps([_row(bidder="Broker A", part_number="LM358N", unit_price="1.2000")])},
        )
        assert resp.status_code == 200
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert "replaced 1 earlier upload" in trigger["showToast"]["message"]
    finally:
        restore()


def test_upload_confirm_no_superseded_note_when_zero(client, db_session, trader_user, posted_list):
    restore = _own(trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-confirm",
            data={"rows_json": json.dumps([_row(bidder="Broker A", part_number="LM358N")])},
        )
        assert resp.status_code == 200
        trigger = json.loads(resp.headers["HX-Trigger"])
        assert "replaced" not in trigger["showToast"]["message"]
    finally:
        restore()


# ---------------------------------------------------------------------------
# Finding #3 (P3) — blank separator rows shift preview row numbers away from the
# spreadsheet's literal row numbers; the template promise is now honest about it.
# ---------------------------------------------------------------------------


def test_upload_preview_blank_separator_row_shifts_numbering(client, db_session, trader_user, posted_list):
    """A blank line between two bidders' rows is dropped by the CSV parser before the
    service ever sees it (file_utils._parse_csv's csv.DictReader skips blank lines), so
    the preview's row numbers count NON-BLANK rows only — Broker B lands on row 3 (its
    file row is actually 4).

    Parsed via the real upload-preview route so the parser's blank-row skip is genuinely
    exercised, not simulated.
    """
    restore = _own(trader_user)
    try:
        csv_bytes = (
            b"Bidder,Part Number,Offer Qty,Unit Price\nBroker A,LM358N,150,1.2000\n\nBroker B,NE555P,50,0.8000\n"
        )
        resp = client.post(
            f"/api/resell/{posted_list.id}/bids/upload-preview",
            files={"file": ("bids.csv", csv_bytes, "text/csv")},
        )
        assert resp.status_code == 200

        from app.file_utils import parse_tabular_file

        parsed_rows = parse_tabular_file(csv_bytes, "bids.csv")
        assert len(parsed_rows) == 2  # the blank line is already gone before the service sees it

        result = excess_service.preview_bid_upload(db_session, posted_list.id, parsed_rows)
        rows_by_bidder = {a["bidder"]: a["row"] for a in result["accepted"]}
        assert rows_by_bidder["Broker A"] == 2  # matches its real file row
        assert rows_by_bidder["Broker B"] == 3  # its real file row is 4 — drift is pinned, not hidden
    finally:
        restore()

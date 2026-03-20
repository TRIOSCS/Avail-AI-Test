"""test_excess_crud.py — Tests for excess inventory service layer.

Covers CRUD operations on ExcessList and bulk import of line items
with flexible header detection.

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, conftest fixtures
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList
from app.services.excess_service import (
    accept_bid,
    confirm_import,
    create_bid,
    create_excess_list,
    delete_excess_list,
    get_excess_list,
    import_line_items,
    list_bids,
    list_excess_lists,
    preview_import,
    update_excess_list,
)
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_company(db: Session, name: str = "Seller Corp") -> Company:
    co = Company(name=name)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_user(db: Session, email: str = "trader@test.com") -> User:
    user = User(
        email=email,
        name="Test Trader",
        role="trader",
        azure_id=f"az-{email}",
        m365_connected=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_excess_list(db: Session, company: Company, user: User, title: str = "Test Excess") -> ExcessList:
    return create_excess_list(db, title=title, company_id=company.id, owner_id=user.id)


@pytest.fixture()
def company(db_session: Session) -> Company:
    return _make_company(db_session)


@pytest.fixture()
def trader(db_session: Session) -> User:
    return _make_user(db_session)


# ---------------------------------------------------------------------------
# TestCreateExcessList
# ---------------------------------------------------------------------------


class TestCreateExcessList:
    def test_create_with_required_fields(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        el = create_excess_list(
            db_session,
            title="Q1 Excess",
            company_id=company.id,
            owner_id=user.id,
        )

        assert el.id is not None
        assert el.title == "Q1 Excess"
        assert el.company_id == company.id
        assert el.owner_id == user.id
        assert el.status == "draft"
        assert el.total_line_items == 0

    def test_create_with_optional_fields(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        el = create_excess_list(
            db_session,
            title="Full Excess",
            company_id=company.id,
            owner_id=user.id,
            notes="Some notes",
            source_filename="excess.csv",
        )

        assert el.notes == "Some notes"
        assert el.source_filename == "excess.csv"

    def test_invalid_company_raises_404(self, db_session: Session):
        user = _make_user(db_session)

        with pytest.raises(HTTPException) as exc_info:
            create_excess_list(
                db_session,
                title="Bad Company",
                company_id=99999,
                owner_id=user.id,
            )
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestGetExcessList
# ---------------------------------------------------------------------------


class TestGetExcessList:
    def test_get_existing(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        fetched = get_excess_list(db_session, el.id)
        assert fetched.id == el.id
        assert fetched.title == "Test Excess"

    def test_get_not_found_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            get_excess_list(db_session, 99999)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestListExcessLists
# ---------------------------------------------------------------------------


class TestListExcessLists:
    def test_returns_paginated(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        for i in range(3):
            _make_excess_list(db_session, company, user, title=f"List {i}")

        result = list_excess_lists(db_session, limit=2, offset=0)

        assert result["total"] == 3
        assert len(result["items"]) == 2
        assert result["limit"] == 2
        assert result["offset"] == 0

    def test_search_filter(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        _make_excess_list(db_session, company, user, title="Alpha Batch")
        _make_excess_list(db_session, company, user, title="Beta Batch")
        _make_excess_list(db_session, company, user, title="Gamma Set")

        result = list_excess_lists(db_session, q="batch")
        assert result["total"] == 2

    def test_status_filter(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        el = _make_excess_list(db_session, company, user, title="Active One")
        update_excess_list(db_session, el.id, status="active")
        _make_excess_list(db_session, company, user, title="Draft One")

        result = list_excess_lists(db_session, status="active")
        assert result["total"] == 1
        assert result["items"][0].title == "Active One"


# ---------------------------------------------------------------------------
# TestUpdateExcessList
# ---------------------------------------------------------------------------


class TestUpdateExcessList:
    def test_updates_title(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        updated = update_excess_list(db_session, el.id, title="New Title")
        assert updated.title == "New Title"

    def test_not_found_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            update_excess_list(db_session, 99999, title="Nope")
        assert exc_info.value.status_code == 404

    def test_ignores_none_values(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user, title="Original")

        updated = update_excess_list(db_session, el.id, title=None, notes="Added")
        assert updated.title == "Original"
        assert updated.notes == "Added"


# ---------------------------------------------------------------------------
# TestDeleteExcessList
# ---------------------------------------------------------------------------


class TestDeleteExcessList:
    def test_hard_deletes(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        list_id = el.id

        delete_excess_list(db_session, list_id)

        with pytest.raises(HTTPException) as exc_info:
            get_excess_list(db_session, list_id)
        assert exc_info.value.status_code == 404

    def test_delete_not_found_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            delete_excess_list(db_session, 99999)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestImportLineItems
# ---------------------------------------------------------------------------


class TestImportLineItems:
    def test_imports_valid_rows(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        rows = [
            {"part_number": "LM317T", "quantity": "100", "manufacturer": "TI", "asking_price": "0.50"},
            {"part_number": "NE555P", "quantity": "200", "manufacturer": "TI"},
        ]

        result = import_line_items(db_session, el.id, rows)

        assert result["imported"] == 2
        assert result["skipped"] == 0
        assert result["errors"] == []

        # Verify counter updated
        db_session.refresh(el)
        assert el.total_line_items == 2

        # Verify items in DB
        items = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).all()
        assert len(items) == 2

    def test_flexible_headers(self, db_session: Session):
        """Accepts mpn/qty/price/mfr/dc/cond aliases."""
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        rows = [
            {"mpn": "LM317T", "qty": "50", "mfr": "Texas Instruments", "price": "$1.25", "dc": "2024+", "cond": "New"},
        ]

        result = import_line_items(db_session, el.id, rows)

        assert result["imported"] == 1

        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        assert item.part_number == "LM317T"
        assert item.quantity == 50
        assert item.manufacturer == "Texas Instruments"
        assert item.date_code == "2024+"
        assert item.condition == "New"
        assert float(item.asking_price) == 1.25

    def test_skips_blank_part_number(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        rows = [
            {"part_number": "", "quantity": "100"},
            {"part_number": "  ", "quantity": "100"},
            {"part_number": "LM317T", "quantity": "100"},
        ]

        result = import_line_items(db_session, el.id, rows)

        assert result["imported"] == 1
        assert result["skipped"] == 2
        assert len(result["errors"]) == 2

    def test_skips_invalid_quantity(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        rows = [
            {"part_number": "LM317T", "quantity": "abc"},
            {"part_number": "NE555P", "quantity": "-5"},
            {"part_number": "LM7805", "quantity": "0"},
            {"part_number": "AD620", "quantity": "50"},
        ]

        result = import_line_items(db_session, el.id, rows)

        assert result["imported"] == 1
        assert result["skipped"] == 3

    def test_updates_total_counter(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        # First import
        import_line_items(db_session, el.id, [{"part_number": "A", "quantity": "1"}])
        db_session.refresh(el)
        assert el.total_line_items == 1

        # Second import adds to counter
        import_line_items(
            db_session, el.id, [{"part_number": "B", "quantity": "2"}, {"part_number": "C", "quantity": "3"}]
        )
        db_session.refresh(el)
        assert el.total_line_items == 3

    def test_not_found_list_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            import_line_items(db_session, 99999, [{"part_number": "X", "quantity": "1"}])
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestPreviewImport
# ---------------------------------------------------------------------------


class TestPreviewImport:
    def test_parses_valid_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Preview", company_id=company.id, owner_id=trader.id)
        rows = [
            {"part_number": "LM358N", "quantity": "500", "asking_price": "0.45"},
            {"mpn": "NE555P", "qty": "1000", "manufacturer": "TI"},
        ]
        result = preview_import(rows)
        assert result["valid_count"] == 2
        assert result["error_count"] == 0
        assert len(result["preview_rows"]) == 2
        assert result["preview_rows"][0]["part_number"] == "LM358N"

    def test_flags_invalid_rows(self):
        rows = [
            {"part_number": "", "quantity": "500"},
            {"part_number": "LM358N", "quantity": "abc"},
            {"part_number": "NE555P", "quantity": "100"},
        ]
        result = preview_import(rows)
        assert result["valid_count"] == 1
        assert result["error_count"] == 2
        assert len(result["errors"]) == 2
        assert "Row 1" in result["errors"][0]

    def test_detects_column_mapping(self):
        rows = [{"mpn": "LM358N", "qty": "100", "cost": "0.50"}]
        result = preview_import(rows)
        mapping = result["column_mapping"]
        assert mapping["mpn"] == "part_number"
        assert mapping["qty"] == "quantity"
        assert mapping["cost"] == "asking_price"

    def test_limits_preview_to_10_rows(self):
        rows = [{"part_number": f"PART{i}", "quantity": "1"} for i in range(25)]
        result = preview_import(rows)
        assert len(result["preview_rows"]) == 10
        assert result["valid_count"] == 25


# ---------------------------------------------------------------------------
# TestConfirmImport
# ---------------------------------------------------------------------------


class TestConfirmImport:
    def test_imports_validated_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Confirm", company_id=company.id, owner_id=trader.id)
        validated_rows = [
            {"part_number": "LM358N", "quantity": 500, "asking_price": 0.45},
            {"part_number": "NE555P", "quantity": 1000, "manufacturer": "TI"},
        ]
        result = confirm_import(db_session, el.id, validated_rows)
        assert result["imported"] == 2
        db_session.refresh(el)
        assert el.total_line_items == 2

    def test_rejects_empty_rows(self, db_session, company, trader):
        el = create_excess_list(db_session, title="Empty", company_id=company.id, owner_id=trader.id)
        result = confirm_import(db_session, el.id, [])
        assert result["imported"] == 0


# ---------------------------------------------------------------------------
# TestMatchExcessDemand
# ---------------------------------------------------------------------------


class TestMatchExcessDemand:
    @pytest.fixture()
    def active_req(self, db_session, company, trader):
        """Create an active requisition with a requirement for LM358N."""
        from app.models.sourcing import Requirement, Requisition
        from app.utils.normalization import normalize_mpn_key

        req = Requisition(name="Test RFQ", status="active", created_by=trader.id, company_id=company.id)
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="LM358N",
            normalized_mpn=normalize_mpn_key("LM358N"),
            target_qty=100,
        )
        db_session.add(requirement)
        db_session.commit()
        return req, requirement

    def test_creates_offer_on_match(self, db_session, company, trader, active_req):
        from app.models.offers import Offer
        from app.services.excess_service import match_excess_demand

        req, requirement = active_req
        el = create_excess_list(db_session, title="Match Test", company_id=company.id, owner_id=trader.id)
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500, "asking_price": 0.45}])
        result = match_excess_demand(db_session, el.id, user_id=trader.id)
        assert result["matches_created"] >= 1
        offer = db_session.query(Offer).filter(Offer.source == "excess", Offer.requisition_id == req.id).first()
        assert offer is not None
        assert offer.mpn == "LM358N"
        assert float(offer.unit_price) == 0.45
        assert offer.vendor_name == company.name

    def test_updates_demand_match_count(self, db_session, company, trader, active_req):
        from app.services.excess_service import match_excess_demand

        el = create_excess_list(db_session, title="Count Test", company_id=company.id, owner_id=trader.id)
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500}])
        match_excess_demand(db_session, el.id, user_id=trader.id)
        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        assert item.demand_match_count >= 1

    def test_no_match_for_unrelated_part(self, db_session, company, trader, active_req):
        from app.services.excess_service import match_excess_demand

        el = create_excess_list(db_session, title="No Match", company_id=company.id, owner_id=trader.id)
        confirm_import(db_session, el.id, [{"part_number": "XXXXXX", "quantity": 100}])
        result = match_excess_demand(db_session, el.id, user_id=trader.id)
        assert result["matches_created"] == 0

    def test_skips_archived_requisitions(self, db_session, company, trader):
        from app.models.sourcing import Requirement, Requisition
        from app.services.excess_service import match_excess_demand
        from app.utils.normalization import normalize_mpn_key

        req = Requisition(name="Old RFQ", status="archived", created_by=trader.id, company_id=company.id)
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="LM358N",
            normalized_mpn=normalize_mpn_key("LM358N"),
            target_qty=100,
        )
        db_session.add(requirement)
        db_session.commit()

        el = create_excess_list(db_session, title="Archived", company_id=company.id, owner_id=trader.id)
        confirm_import(db_session, el.id, [{"part_number": "LM358N", "quantity": 500}])
        result = match_excess_demand(db_session, el.id, user_id=trader.id)
        assert result["matches_created"] == 0


# ---------------------------------------------------------------------------
# Bid helpers
# ---------------------------------------------------------------------------


def _make_line_item(db: Session, excess_list: ExcessList, part_number: str = "LM317T", quantity: int = 100):
    item = ExcessLineItem(
        excess_list_id=excess_list.id,
        part_number=part_number,
        quantity=quantity,
        asking_price=1.50,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# ---------------------------------------------------------------------------
# TestCreateBid
# ---------------------------------------------------------------------------


class TestCreateBid:
    def test_creates_bid_with_required_fields(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        bid = create_bid(
            db_session,
            line_item_id=item.id,
            list_id=el.id,
            unit_price=1.25,
            quantity_wanted=50,
            user_id=user.id,
        )

        assert bid.id is not None
        assert float(bid.unit_price) == 1.25
        assert bid.quantity_wanted == 50
        assert bid.status == "pending"
        assert bid.source == "manual"
        assert bid.created_by == user.id

    def test_creates_bid_with_all_fields(self, db_session: Session):
        company = _make_company(db_session)
        buyer_company = _make_company(db_session, name="Buyer Corp")
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        bid = create_bid(
            db_session,
            line_item_id=item.id,
            list_id=el.id,
            unit_price=2.00,
            quantity_wanted=100,
            user_id=user.id,
            bidder_company_id=buyer_company.id,
            lead_time_days=5,
            source="phone",
            notes="Urgent order",
        )

        assert bid.bidder_company_id == buyer_company.id
        assert bid.lead_time_days == 5
        assert bid.source == "phone"
        assert bid.notes == "Urgent order"

    def test_invalid_line_item_raises_404(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        with pytest.raises(HTTPException) as exc_info:
            create_bid(
                db_session,
                line_item_id=99999,
                list_id=el.id,
                unit_price=1.00,
                quantity_wanted=10,
                user_id=user.id,
            )
        assert exc_info.value.status_code == 404

    def test_item_not_in_list_raises_404(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el1 = _make_excess_list(db_session, company, user, title="List A")
        el2 = _make_excess_list(db_session, company, user, title="List B")
        item = _make_line_item(db_session, el1)

        with pytest.raises(HTTPException) as exc_info:
            create_bid(
                db_session,
                line_item_id=item.id,
                list_id=el2.id,
                unit_price=1.00,
                quantity_wanted=10,
                user_id=user.id,
            )
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestListBids
# ---------------------------------------------------------------------------


class TestListBids:
    def test_returns_bids_sorted_by_price(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=3.00, quantity_wanted=10, user_id=user.id
        )
        create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=1.00, quantity_wanted=20, user_id=user.id
        )
        create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=2.00, quantity_wanted=30, user_id=user.id
        )

        bids = list_bids(db_session, item.id, el.id)
        assert len(bids) == 3
        prices = [float(b.unit_price) for b in bids]
        assert prices == [1.00, 2.00, 3.00]

    def test_empty_list_returns_empty(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        bids = list_bids(db_session, item.id, el.id)
        assert bids == []

    def test_invalid_item_raises_404(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        with pytest.raises(HTTPException) as exc_info:
            list_bids(db_session, 99999, el.id)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# TestAcceptBid
# ---------------------------------------------------------------------------


class TestAcceptBid:
    def test_accepts_bid_and_rejects_others(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        bid1 = create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=1.00, quantity_wanted=10, user_id=user.id
        )
        bid2 = create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=2.00, quantity_wanted=20, user_id=user.id
        )
        bid3 = create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=3.00, quantity_wanted=30, user_id=user.id
        )

        accepted = accept_bid(db_session, bid1.id, item.id, el.id)

        assert accepted.status == "accepted"

        db_session.refresh(bid2)
        db_session.refresh(bid3)
        assert bid2.status == "rejected"
        assert bid3.status == "rejected"

        db_session.refresh(item)
        assert item.status == "awarded"

    def test_accepts_bid_preserves_non_pending(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        bid1 = create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=1.00, quantity_wanted=10, user_id=user.id
        )
        bid2 = create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=2.00, quantity_wanted=20, user_id=user.id
        )

        # Manually set bid2 to withdrawn before accepting bid1
        bid2.status = "withdrawn"
        db_session.commit()

        accept_bid(db_session, bid1.id, item.id, el.id)

        db_session.refresh(bid2)
        assert bid2.status == "withdrawn"  # Should NOT be changed to rejected

    def test_invalid_bid_raises_404(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        with pytest.raises(HTTPException) as exc_info:
            accept_bid(db_session, 99999, item.id, el.id)
        assert exc_info.value.status_code == 404

    def test_bid_wrong_item_raises_404(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item1 = _make_line_item(db_session, el, part_number="PART-A")
        item2 = _make_line_item(db_session, el, part_number="PART-B")

        bid = create_bid(
            db_session, line_item_id=item1.id, list_id=el.id, unit_price=1.00, quantity_wanted=10, user_id=user.id
        )

        with pytest.raises(HTTPException) as exc_info:
            accept_bid(db_session, bid.id, item2.id, el.id)
        assert exc_info.value.status_code == 404

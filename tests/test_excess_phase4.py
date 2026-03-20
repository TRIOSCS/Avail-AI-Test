"""test_excess_phase4.py — Tests for Phase 4 Excess Resell features.

Covers:
- Stats endpoint (total lists, items, pending bids, matched items)
- Normalization display (normalized_part_number on line items)
- Offer.excess_line_item_id FK linkage
- ProactiveMatch creation for archived deals
- Email bid solicitations (send + parse response)
- Note tooltips (notes field in responses)

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, conftest fixtures
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, User
from app.models.excess import ExcessLineItem, ExcessList
from app.models.intelligence import ProactiveMatch
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.services.excess_service import (
    backfill_normalized_part_numbers,
    confirm_import,
    create_bid,
    create_excess_list,
    create_proactive_matches_for_excess,
    get_excess_stats,
    list_solicitations,
    match_excess_demand,
    parse_bid_response,
    send_bid_solicitation,
    update_excess_list,
)
from app.utils.normalization import normalize_mpn_key
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


def _make_line_item(
    db: Session, excess_list: ExcessList, part_number: str = "LM317T", quantity: int = 100
) -> ExcessLineItem:
    item = ExcessLineItem(
        excess_list_id=excess_list.id,
        part_number=part_number,
        normalized_part_number=normalize_mpn_key(part_number) or None,
        quantity=quantity,
        asking_price=1.50,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_customer_site(db: Session, company: Company) -> CustomerSite:
    site = CustomerSite(
        company_id=company.id,
        site_name="Main Site",
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


@pytest.fixture()
def company(db_session: Session) -> Company:
    return _make_company(db_session)


@pytest.fixture()
def trader(db_session: Session) -> User:
    return _make_user(db_session)


# ---------------------------------------------------------------------------
# TestExcessStats
# ---------------------------------------------------------------------------


class TestExcessStats:
    def test_empty_stats(self, db_session: Session):
        stats = get_excess_stats(db_session)
        assert stats["total_lists"] == 0
        assert stats["total_line_items"] == 0
        assert stats["pending_bids"] == 0
        assert stats["matched_items"] == 0
        assert stats["total_bids"] == 0
        assert stats["awarded_items"] == 0

    def test_counts_lists_and_items(self, db_session: Session, company, trader):
        el = _make_excess_list(db_session, company, trader)
        _make_line_item(db_session, el, "PART-A")
        _make_line_item(db_session, el, "PART-B")

        stats = get_excess_stats(db_session)
        assert stats["total_lists"] == 1
        assert stats["total_line_items"] == 2

    def test_counts_bids(self, db_session: Session, company, trader):
        el = _make_excess_list(db_session, company, trader)
        item = _make_line_item(db_session, el)

        create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=1.0, quantity_wanted=10, user_id=trader.id
        )
        create_bid(
            db_session, line_item_id=item.id, list_id=el.id, unit_price=2.0, quantity_wanted=20, user_id=trader.id
        )

        stats = get_excess_stats(db_session)
        assert stats["pending_bids"] == 2
        assert stats["total_bids"] == 2

    def test_counts_matched_items(self, db_session: Session, company, trader):
        el = _make_excess_list(db_session, company, trader)
        item = _make_line_item(db_session, el)
        item.demand_match_count = 3
        db_session.commit()

        stats = get_excess_stats(db_session)
        assert stats["matched_items"] == 1

    def test_counts_awarded_items(self, db_session: Session, company, trader):
        el = _make_excess_list(db_session, company, trader)
        item = _make_line_item(db_session, el)
        item.status = "awarded"
        db_session.commit()

        stats = get_excess_stats(db_session)
        assert stats["awarded_items"] == 1


# ---------------------------------------------------------------------------
# TestNormalizationDisplay
# ---------------------------------------------------------------------------


class TestNormalizationDisplay:
    def test_import_sets_normalized_part_number(self, db_session: Session, company, trader):
        el = _make_excess_list(db_session, company, trader)
        confirm_import(db_session, el.id, [{"part_number": "LM-358N", "quantity": 100}])

        item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        assert item.normalized_part_number == "lm358n"
        assert item.part_number == "LM-358N"

    def test_manual_add_sets_normalized(self, db_session: Session, company, trader):
        el = _make_excess_list(db_session, company, trader)
        item = _make_line_item(db_session, el, part_number="AD-620AR")
        assert item.normalized_part_number == "ad620ar"

    def test_backfill_normalized_part_numbers(self, db_session: Session, company, trader):
        el = _make_excess_list(db_session, company, trader)
        # Create item without normalized_part_number (simulating pre-Phase4 data)
        item = ExcessLineItem(
            excess_list_id=el.id,
            part_number="LM7805CT",
            quantity=50,
        )
        db_session.add(item)
        db_session.commit()

        assert item.normalized_part_number is None

        count = backfill_normalized_part_numbers(db_session)
        assert count == 1

        db_session.refresh(item)
        assert item.normalized_part_number == "lm7805ct"


# ---------------------------------------------------------------------------
# TestOfferExcessLineItemFK
# ---------------------------------------------------------------------------


class TestOfferExcessLineItemFK:
    def test_demand_match_sets_excess_line_item_id(self, db_session: Session, company, trader):
        """When demand matching creates an Offer, it should set excess_line_item_id."""
        req = Requisition(name="Test RFQ", status="active", created_by=trader.id, company_id=company.id)
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            normalized_mpn=normalize_mpn_key("LM317T"),
            target_qty=100,
        )
        db_session.add(requirement)
        db_session.commit()

        el = _make_excess_list(db_session, company, trader)
        confirm_import(db_session, el.id, [{"part_number": "LM317T", "quantity": 500, "asking_price": 0.45}])
        match_excess_demand(db_session, el.id, user_id=trader.id)

        offer = db_session.query(Offer).filter(Offer.source == "excess").first()
        assert offer is not None
        assert offer.excess_line_item_id is not None

        line_item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
        assert offer.excess_line_item_id == line_item.id


# ---------------------------------------------------------------------------
# TestBidSolicitations
# ---------------------------------------------------------------------------


class TestBidSolicitations:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.GraphClient")
    async def test_send_solicitation_creates_records(self, mock_gc_cls, db_session: Session, company, trader):
        mock_gc_cls.return_value.post_json = AsyncMock(return_value={})
        el = _make_excess_list(db_session, company, trader)
        item1 = _make_line_item(db_session, el, "PART-A")
        item2 = _make_line_item(db_session, el, "PART-B")

        solicitations = await send_bid_solicitation(
            db_session,
            list_id=el.id,
            line_item_ids=[item1.id, item2.id],
            recipient_email="buyer@example.com",
            recipient_name="John Buyer",
            contact_id=1,
            user_id=trader.id,
            token="fake",
        )

        assert len(solicitations) == 2
        assert solicitations[0].recipient_email == "buyer@example.com"
        assert solicitations[0].recipient_name == "John Buyer"
        assert solicitations[0].status == "sent"
        assert solicitations[0].sent_at is not None

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.GraphClient")
    async def test_send_solicitation_with_custom_subject(self, mock_gc_cls, db_session: Session, company, trader):
        mock_gc_cls.return_value.post_json = AsyncMock(return_value={})
        el = _make_excess_list(db_session, company, trader)
        item = _make_line_item(db_session, el)

        solicitations = await send_bid_solicitation(
            db_session,
            list_id=el.id,
            line_item_ids=[item.id],
            recipient_email="buyer@example.com",
            recipient_name=None,
            contact_id=1,
            user_id=trader.id,
            token="fake",
            subject="Custom Subject",
            message="Custom message body",
        )

        assert "Custom Subject" in solicitations[0].subject
        assert solicitations[0].body_preview == "Custom message body"

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.GraphClient")
    async def test_send_solicitation_invalid_item(self, mock_gc_cls, db_session: Session, company, trader):
        el = _make_excess_list(db_session, company, trader)

        with pytest.raises(HTTPException) as exc_info:
            await send_bid_solicitation(
                db_session,
                list_id=el.id,
                line_item_ids=[99999],
                recipient_email="buyer@example.com",
                recipient_name=None,
                contact_id=1,
                user_id=trader.id,
                token="fake",
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.GraphClient")
    async def test_parse_bid_response_creates_bid(self, mock_gc_cls, db_session: Session, company, trader):
        mock_gc_cls.return_value.post_json = AsyncMock(return_value={})
        el = _make_excess_list(db_session, company, trader)
        item = _make_line_item(db_session, el)

        solicitations = await send_bid_solicitation(
            db_session,
            list_id=el.id,
            line_item_ids=[item.id],
            recipient_email="buyer@example.com",
            recipient_name=None,
            contact_id=1,
            user_id=trader.id,
            token="fake",
        )

        bid = parse_bid_response(
            db_session,
            solicitation_id=solicitations[0].id,
            unit_price=1.25,
            quantity_wanted=50,
            lead_time_days=5,
        )

        assert bid.id is not None
        assert float(bid.unit_price) == 1.25
        assert bid.quantity_wanted == 50
        assert bid.source == "email_parsed"
        assert bid.lead_time_days == 5

        db_session.refresh(solicitations[0])
        assert solicitations[0].status == "responded"
        assert solicitations[0].parsed_bid_id == bid.id
        assert solicitations[0].response_received_at is not None

    def test_parse_bid_response_invalid_solicitation(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            parse_bid_response(db_session, solicitation_id=99999, unit_price=1.0, quantity_wanted=10)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.GraphClient")
    async def test_list_solicitations(self, mock_gc_cls, db_session: Session, company, trader):
        mock_gc_cls.return_value.post_json = AsyncMock(return_value={})
        el = _make_excess_list(db_session, company, trader)
        item1 = _make_line_item(db_session, el, "A")
        item2 = _make_line_item(db_session, el, "B")

        await send_bid_solicitation(
            db_session,
            list_id=el.id,
            line_item_ids=[item1.id],
            recipient_email="a@test.com",
            recipient_name=None,
            contact_id=1,
            user_id=trader.id,
            token="fake",
        )
        await send_bid_solicitation(
            db_session,
            list_id=el.id,
            line_item_ids=[item2.id],
            recipient_email="b@test.com",
            recipient_name=None,
            contact_id=2,
            user_id=trader.id,
            token="fake",
        )

        # All solicitations for the list
        all_s = list_solicitations(db_session, el.id)
        assert len(all_s) == 2

        # Filter by item
        item1_s = list_solicitations(db_session, el.id, item1.id)
        assert len(item1_s) == 1


# ---------------------------------------------------------------------------
# TestProactiveMatchForArchived
# ---------------------------------------------------------------------------


class TestProactiveMatchForArchived:
    def test_no_matches_for_active_list(self, db_session: Session, company, trader):
        el = _make_excess_list(db_session, company, trader)
        _make_line_item(db_session, el)

        result = create_proactive_matches_for_excess(db_session, el.id, user_id=trader.id)
        assert result["matches_created"] == 0

    def test_creates_matches_for_closed_list(self, db_session: Session, company, trader):
        # Create customer site (required for ProactiveMatch)
        site = _make_customer_site(db_session, company)

        # Create an archived requisition with a matching requirement
        req = Requisition(name="Old RFQ", status="archived", created_by=trader.id, company_id=company.id)
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            normalized_mpn=normalize_mpn_key("LM317T"),
            target_qty=100,
        )
        db_session.add(requirement)
        db_session.commit()

        # Create and close excess list
        el = _make_excess_list(db_session, company, trader)
        _make_line_item(db_session, el, "LM317T")
        update_excess_list(db_session, el.id, status="closed")

        result = create_proactive_matches_for_excess(db_session, el.id, user_id=trader.id)
        assert result["matches_created"] >= 1

        pm = db_session.query(ProactiveMatch).first()
        assert pm is not None
        assert pm.mpn == "LM317T"
        assert pm.status == "new"

    def test_skips_duplicate_proactive_matches(self, db_session: Session, company, trader):
        site = _make_customer_site(db_session, company)

        req = Requisition(name="Old RFQ", status="archived", created_by=trader.id, company_id=company.id)
        db_session.add(req)
        db_session.flush()
        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            normalized_mpn=normalize_mpn_key("LM317T"),
            target_qty=100,
        )
        db_session.add(requirement)
        db_session.commit()

        el = _make_excess_list(db_session, company, trader)
        _make_line_item(db_session, el, "LM317T")
        update_excess_list(db_session, el.id, status="closed")

        # First run
        result1 = create_proactive_matches_for_excess(db_session, el.id, user_id=trader.id)
        # Second run — should not create duplicates
        result2 = create_proactive_matches_for_excess(db_session, el.id, user_id=trader.id)
        assert result2["matches_created"] == 0

        total = db_session.query(ProactiveMatch).count()
        assert total == result1["matches_created"]


# ---------------------------------------------------------------------------
# TestNoteTooltips
# ---------------------------------------------------------------------------


class TestNoteTooltips:
    def test_notes_included_in_line_item_response(self, db_session: Session, company, trader):
        """Notes field should be available for tooltip display."""
        el = _make_excess_list(db_session, company, trader)
        item = ExcessLineItem(
            excess_list_id=el.id,
            part_number="LM317T",
            normalized_part_number=normalize_mpn_key("LM317T"),
            quantity=100,
            asking_price=1.50,
            notes="Customer needs fast turnaround. Condition verified.",
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)

        from app.schemas.excess import ExcessLineItemResponse

        resp = ExcessLineItemResponse.model_validate(item)
        assert resp.notes == "Customer needs fast turnaround. Condition verified."
        assert resp.normalized_part_number == "lm317t"
        assert resp.demand_match_count == 0


# ---------------------------------------------------------------------------
# TestStatsEndpoint
# ---------------------------------------------------------------------------


class TestStatsEndpoint:
    def test_stats_api_returns_json(self, client, db_session: Session, test_user):
        response = client.get("/api/excess-stats")
        assert response.status_code == 200
        data = response.json()
        assert "total_lists" in data
        assert "total_line_items" in data
        assert "pending_bids" in data
        assert "matched_items" in data
        assert "total_bids" in data
        assert "awarded_items" in data


# ---------------------------------------------------------------------------
# TestSolicitationEndpoints
# ---------------------------------------------------------------------------


class TestSolicitationEndpoints:
    @patch("app.utils.graph_client.GraphClient")
    def test_send_solicitation_endpoint(self, mock_gc_cls, client, db_session: Session, test_user):
        mock_gc_cls.return_value.post_json = AsyncMock(return_value={})
        company = _make_company(db_session, "Endpoint Test Co")
        el = create_excess_list(db_session, title="Endpoint Test", company_id=company.id, owner_id=test_user.id)
        item = _make_line_item(db_session, el)

        response = client.post(
            f"/api/excess-lists/{el.id}/solicitations",
            json={
                "line_item_ids": [item.id],
                "recipient_email": "buyer@test.com",
                "recipient_name": "Test Buyer",
                "contact_id": 1,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["total"] == 1
        assert data["items"][0]["recipient_email"] == "buyer@test.com"

    @patch("app.utils.graph_client.GraphClient")
    def test_list_solicitations_endpoint(self, mock_gc_cls, client, db_session: Session, test_user):
        mock_gc_cls.return_value.post_json = AsyncMock(return_value={})
        company = _make_company(db_session, "List Solicit Co")
        el = create_excess_list(db_session, title="List Test", company_id=company.id, owner_id=test_user.id)
        item = _make_line_item(db_session, el)

        # Create a solicitation via the endpoint (which handles async)
        client.post(
            f"/api/excess-lists/{el.id}/solicitations",
            json={
                "line_item_ids": [item.id],
                "recipient_email": "x@test.com",
                "recipient_name": None,
                "contact_id": 1,
            },
        )

        response = client.get(f"/api/excess-lists/{el.id}/solicitations")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1

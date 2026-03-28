"""test_excess_service_comprehensive.py — Comprehensive tests for excess_service.py.

Covers: get_excess_stats, backfill_normalized_part_numbers,
create_proactive_matches_for_excess, _build_solicitation_html,
_build_bundled_solicitation_html, _find_sent_message, send_bid_solicitation,
parse_bid_response, list_solicitations, _call_claude_bid_parse,
parse_bid_from_email, _parse_price edge cases, _parse_quantity edge cases,
_normalize_row edge cases, _safe_commit IntegrityError.

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, conftest fixtures
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.excess import BidSolicitation, ExcessLineItem, ExcessList
from app.services.excess_service import (
    _build_bundled_solicitation_html,
    _build_solicitation_html,
    _normalize_row,
    _parse_price,
    _parse_quantity,
    _safe_commit,
    backfill_normalized_part_numbers,
    create_bid,
    create_excess_list,
    create_proactive_matches_for_excess,
    get_excess_stats,
    list_solicitations,
    parse_bid_from_email,
    parse_bid_response,
    send_bid_solicitation,
)
from tests.conftest import engine

_ = engine


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


def _make_excess_list(db: Session, company: Company, user: User, title: str = "Test Excess", status: str = "draft"):
    el = create_excess_list(db, title=title, company_id=company.id, owner_id=user.id)
    if status != "draft":
        el.status = status
        db.commit()
        db.refresh(el)
    return el


def _make_line_item(
    db: Session, excess_list: ExcessList, part_number: str = "LM317T", quantity: int = 100, asking_price=1.50
):
    item = ExcessLineItem(
        excess_list_id=excess_list.id,
        part_number=part_number,
        quantity=quantity,
        asking_price=asking_price,
        manufacturer="TI",
        condition="New",
        date_code="2024+",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# ---------------------------------------------------------------------------
# _parse_quantity edge cases
# ---------------------------------------------------------------------------


class TestParseQuantity:
    def test_none_returns_none(self):
        assert _parse_quantity(None) is None

    def test_valid_int(self):
        assert _parse_quantity("100") == 100

    def test_valid_float_string(self):
        assert _parse_quantity("100.5") == 100

    def test_comma_separated(self):
        assert _parse_quantity("1,000") == 1000

    def test_zero_returns_none(self):
        assert _parse_quantity("0") is None

    def test_negative_returns_none(self):
        assert _parse_quantity("-5") is None

    def test_invalid_string(self):
        assert _parse_quantity("abc") is None

    def test_empty_string(self):
        assert _parse_quantity("") is None

    def test_whitespace(self):
        assert _parse_quantity("  50  ") == 50


# ---------------------------------------------------------------------------
# _parse_price edge cases
# ---------------------------------------------------------------------------


class TestParsePrice:
    def test_none_returns_none(self):
        assert _parse_price(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_price("") is None

    def test_whitespace_returns_none(self):
        assert _parse_price("   ") is None

    def test_valid_decimal(self):
        assert _parse_price("1.25") == Decimal("1.25")

    def test_dollar_sign(self):
        assert _parse_price("$1.25") == Decimal("1.25")

    def test_comma_separated(self):
        assert _parse_price("$1,234.56") == Decimal("1234.56")

    def test_zero_is_valid(self):
        assert _parse_price("0") == Decimal("0")

    def test_negative_returns_none(self):
        assert _parse_price("-1.50") is None

    def test_invalid_string(self):
        assert _parse_price("abc") is None

    def test_integer_value(self):
        assert _parse_price(5) == Decimal("5")


# ---------------------------------------------------------------------------
# _normalize_row
# ---------------------------------------------------------------------------


class TestNormalizeRow:
    def test_maps_aliases(self):
        raw = {"mpn": "LM317T", "qty": "100", "price": "$1.50"}
        result = _normalize_row(raw)
        assert result["part_number"] == "LM317T"
        assert result["quantity"] == "100"
        assert result["asking_price"] == "$1.50"

    def test_first_match_wins(self):
        """If multiple keys map to same canonical, first one wins."""
        raw = {"part_number": "FIRST", "mpn": "SECOND"}
        result = _normalize_row(raw)
        assert result["part_number"] == "FIRST"

    def test_unknown_keys_ignored(self):
        raw = {"unknown_key": "value", "mpn": "LM317T"}
        result = _normalize_row(raw)
        assert "unknown_key" not in result
        assert result["part_number"] == "LM317T"

    def test_whitespace_in_keys(self):
        raw = {" Part Number ": "LM317T"}
        result = _normalize_row(raw)
        assert result["part_number"] == "LM317T"

    def test_manufacturer_aliases(self):
        raw = {"mfr": "Texas Instruments"}
        result = _normalize_row(raw)
        assert result["manufacturer"] == "Texas Instruments"

    def test_date_code_aliases(self):
        raw = {"dc": "2024+"}
        result = _normalize_row(raw)
        assert result["date_code"] == "2024+"

    def test_condition_aliases(self):
        raw = {"cond": "New"}
        result = _normalize_row(raw)
        assert result["condition"] == "New"


# ---------------------------------------------------------------------------
# _safe_commit
# ---------------------------------------------------------------------------


class TestSafeCommit:
    def test_integrity_error_raises_409(self):
        mock_db = MagicMock()
        mock_db.commit.side_effect = IntegrityError("dup", {}, None)
        with pytest.raises(HTTPException) as exc_info:
            _safe_commit(mock_db, entity="test")
        assert exc_info.value.status_code == 409
        mock_db.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# get_excess_stats
# ---------------------------------------------------------------------------


class TestGetExcessStats:
    def test_empty_db(self, db_session: Session):
        stats = get_excess_stats(db_session)
        assert stats["total_lists"] == 0
        assert stats["total_line_items"] == 0
        assert stats["pending_bids"] == 0
        assert stats["total_bids"] == 0
        assert stats["matched_items"] == 0
        assert stats["awarded_items"] == 0

    def test_with_data(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        # Create a bid
        create_bid(
            db_session,
            line_item_id=item.id,
            list_id=el.id,
            unit_price=1.0,
            quantity_wanted=50,
            user_id=user.id,
        )

        # Mark item as having demand match
        item.demand_match_count = 1
        db_session.commit()

        stats = get_excess_stats(db_session)
        assert stats["total_lists"] == 1
        assert stats["total_line_items"] == 1
        assert stats["pending_bids"] == 1
        assert stats["total_bids"] == 1
        assert stats["matched_items"] == 1
        assert stats["awarded_items"] == 0

    def test_awarded_items(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)
        item.status = "awarded"
        db_session.commit()

        stats = get_excess_stats(db_session)
        assert stats["awarded_items"] == 1


# ---------------------------------------------------------------------------
# backfill_normalized_part_numbers
# ---------------------------------------------------------------------------


class TestBackfillNormalizedPartNumbers:
    def test_backfills_missing(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el, part_number="LM-317T")
        item.normalized_part_number = None
        db_session.commit()

        count = backfill_normalized_part_numbers(db_session)
        assert count == 1

        db_session.refresh(item)
        assert item.normalized_part_number is not None

    def test_no_items_to_backfill(self, db_session: Session):
        count = backfill_normalized_part_numbers(db_session)
        assert count == 0


# ---------------------------------------------------------------------------
# _build_solicitation_html
# ---------------------------------------------------------------------------


class TestBuildSolicitationHtml:
    def test_with_recipient_name(self):
        item = MagicMock()
        item.part_number = "LM317T"
        item.manufacturer = "TI"
        item.quantity = 100
        item.condition = "New"
        item.date_code = "2024+"
        item.asking_price = Decimal("1.50")

        html = _build_solicitation_html(item, "Please send bid.", "John")
        assert "Hi John," in html
        assert "LM317T" in html
        assert "$1.50" in html
        assert "TI" in html

    def test_without_recipient_name(self):
        item = MagicMock()
        item.part_number = "LM317T"
        item.manufacturer = None
        item.quantity = 100
        item.condition = None
        item.date_code = None
        item.asking_price = None

        html = _build_solicitation_html(item, "Body text", None)
        assert "Hello," in html
        assert "LM317T" in html


# ---------------------------------------------------------------------------
# _build_bundled_solicitation_html
# ---------------------------------------------------------------------------


class TestBuildBundledSolicitationHtml:
    def test_multiple_items(self):
        items = []
        for pn in ["LM317T", "NE555P"]:
            item = MagicMock()
            item.part_number = pn
            item.manufacturer = "TI"
            item.quantity = 100
            item.condition = "New"
            item.date_code = "2024+"
            item.asking_price = Decimal("1.50")
            items.append(item)

        html = _build_bundled_solicitation_html(items, "Please review.", "Jane")
        assert "Hi Jane," in html
        assert "LM317T" in html
        assert "NE555P" in html

    def test_without_recipient_name(self):
        item = MagicMock()
        item.part_number = "ABC123"
        item.manufacturer = None
        item.quantity = 50
        item.condition = None
        item.date_code = None
        item.asking_price = None

        html = _build_bundled_solicitation_html([item], "Body", None)
        assert "Hello," in html


# ---------------------------------------------------------------------------
# _find_sent_message
# ---------------------------------------------------------------------------


class TestFindSentMessage:
    @pytest.mark.asyncio
    async def test_finds_matching_message(self):
        from app.services.excess_service import _find_sent_message

        gc = MagicMock()
        gc.get_json = AsyncMock(
            return_value={
                "value": [
                    {"id": "msg1", "conversationId": "conv1", "subject": "Bid Request [EXCESS-BID-1]"},
                ]
            }
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Bid Request [EXCESS-BID-1]")

        assert result is not None
        assert result["id"] == "msg1"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        from app.services.excess_service import _find_sent_message

        gc = MagicMock()
        gc.get_json = AsyncMock(return_value={"value": []})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Nonexistent Subject")

        assert result is None

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        from app.services.excess_service import _find_sent_message

        gc = MagicMock()
        gc.get_json = AsyncMock(side_effect=RuntimeError("API error"))

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _find_sent_message(gc, "Some Subject")

        assert result is None


# ---------------------------------------------------------------------------
# parse_bid_response
# ---------------------------------------------------------------------------


class TestParseBidResponse:
    def test_creates_bid_from_solicitation(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        solicitation = BidSolicitation(
            excess_line_item_id=item.id,
            contact_id=1,
            sent_by=user.id,
            recipient_email="buyer@test.com",
            status="sent",
        )
        db_session.add(solicitation)
        db_session.commit()
        db_session.refresh(solicitation)

        bid = parse_bid_response(
            db_session,
            solicitation_id=solicitation.id,
            unit_price=1.25,
            quantity_wanted=50,
            lead_time_days=5,
            notes="Urgent",
        )

        assert bid.id is not None
        assert float(bid.unit_price) == 1.25
        assert bid.quantity_wanted == 50
        assert bid.source == "email_parsed"

        db_session.refresh(solicitation)
        assert solicitation.status == "responded"
        assert solicitation.parsed_bid_id == bid.id

    def test_not_found_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            parse_bid_response(
                db_session,
                solicitation_id=99999,
                unit_price=1.0,
                quantity_wanted=10,
            )
        assert exc_info.value.status_code == 404

    def test_missing_line_item_raises_404(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)

        solicitation = BidSolicitation(
            excess_line_item_id=99999,
            contact_id=1,
            sent_by=user.id,
            recipient_email="buyer@test.com",
            status="sent",
        )
        db_session.add(solicitation)
        db_session.commit()
        db_session.refresh(solicitation)

        with pytest.raises(HTTPException) as exc_info:
            parse_bid_response(
                db_session,
                solicitation_id=solicitation.id,
                unit_price=1.0,
                quantity_wanted=10,
            )
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# list_solicitations
# ---------------------------------------------------------------------------


class TestListSolicitations:
    def test_returns_solicitations_for_list(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        sol = BidSolicitation(
            excess_line_item_id=item.id,
            contact_id=1,
            sent_by=user.id,
            recipient_email="buyer@test.com",
            status="sent",
        )
        db_session.add(sol)
        db_session.commit()

        result = list_solicitations(db_session, el.id)
        assert len(result) == 1

    def test_filters_by_item_id(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item1 = _make_line_item(db_session, el, part_number="PART-A")
        item2 = _make_line_item(db_session, el, part_number="PART-B")

        sol1 = BidSolicitation(
            excess_line_item_id=item1.id,
            contact_id=1,
            sent_by=user.id,
            recipient_email="a@test.com",
            status="sent",
        )
        sol2 = BidSolicitation(
            excess_line_item_id=item2.id,
            contact_id=2,
            sent_by=user.id,
            recipient_email="b@test.com",
            status="sent",
        )
        db_session.add_all([sol1, sol2])
        db_session.commit()

        result = list_solicitations(db_session, el.id, item_id=item1.id)
        assert len(result) == 1

    def test_not_found_list_raises_404(self, db_session: Session):
        with pytest.raises(HTTPException) as exc_info:
            list_solicitations(db_session, 99999)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# _call_claude_bid_parse
# ---------------------------------------------------------------------------


class TestCallClaudeBidParse:
    @pytest.mark.asyncio
    async def test_returns_claude_response(self):
        from app.services.excess_service import _call_claude_bid_parse

        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value='{"unit_price": 1.5}'):
            result = await _call_claude_bid_parse("Some email body")
        assert "unit_price" in result

    @pytest.mark.asyncio
    async def test_returns_empty_on_none(self):
        from app.services.excess_service import _call_claude_bid_parse

        with patch("app.utils.claude_client.claude_text", new_callable=AsyncMock, return_value=None):
            result = await _call_claude_bid_parse("Some email body")
        assert result == ""


# ---------------------------------------------------------------------------
# parse_bid_from_email
# ---------------------------------------------------------------------------


class TestParseBidFromEmail:
    @pytest.mark.asyncio
    async def test_successful_parse(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        sol = BidSolicitation(
            excess_line_item_id=item.id,
            contact_id=1,
            sent_by=user.id,
            recipient_email="buyer@test.com",
            status="sent",
        )
        db_session.add(sol)
        db_session.commit()
        db_session.refresh(sol)

        claude_response = '{"unit_price": 1.25, "quantity_wanted": 50, "lead_time_days": 5}'
        with patch(
            "app.services.excess_service._call_claude_bid_parse",
            new_callable=AsyncMock,
            return_value=claude_response,
        ):
            bid = await parse_bid_from_email(db_session, sol.id, "We offer $1.25 for 50 pcs")

        assert bid is not None
        assert float(bid.unit_price) == 1.25
        assert bid.quantity_wanted == 50

    @pytest.mark.asyncio
    async def test_declined_bid(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        sol = BidSolicitation(
            excess_line_item_id=item.id,
            contact_id=1,
            sent_by=user.id,
            recipient_email="buyer@test.com",
            status="sent",
        )
        db_session.add(sol)
        db_session.commit()
        db_session.refresh(sol)

        with patch(
            "app.services.excess_service._call_claude_bid_parse",
            new_callable=AsyncMock,
            return_value='{"declined": true}',
        ):
            bid = await parse_bid_from_email(db_session, sol.id, "Sorry, not interested")

        assert bid is None
        db_session.refresh(sol)
        assert sol.status == "responded"

    @pytest.mark.asyncio
    async def test_invalid_json(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        sol = BidSolicitation(
            excess_line_item_id=item.id,
            contact_id=1,
            sent_by=user.id,
            recipient_email="buyer@test.com",
            status="sent",
        )
        db_session.add(sol)
        db_session.commit()
        db_session.refresh(sol)

        with patch(
            "app.services.excess_service._call_claude_bid_parse",
            new_callable=AsyncMock,
            return_value="not valid json",
        ):
            bid = await parse_bid_from_email(db_session, sol.id, "Some email")

        assert bid is None

    @pytest.mark.asyncio
    async def test_incomplete_data(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        sol = BidSolicitation(
            excess_line_item_id=item.id,
            contact_id=1,
            sent_by=user.id,
            recipient_email="buyer@test.com",
            status="sent",
        )
        db_session.add(sol)
        db_session.commit()
        db_session.refresh(sol)

        with patch(
            "app.services.excess_service._call_claude_bid_parse",
            new_callable=AsyncMock,
            return_value='{"unit_price": 1.25}',
        ):
            bid = await parse_bid_from_email(db_session, sol.id, "Some email")

        assert bid is None

    @pytest.mark.asyncio
    async def test_solicitation_not_found(self, db_session: Session):
        bid = await parse_bid_from_email(db_session, 99999, "Some email")
        assert bid is None

    @pytest.mark.asyncio
    async def test_strips_markdown_fences(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item = _make_line_item(db_session, el)

        sol = BidSolicitation(
            excess_line_item_id=item.id,
            contact_id=1,
            sent_by=user.id,
            recipient_email="buyer@test.com",
            status="sent",
        )
        db_session.add(sol)
        db_session.commit()
        db_session.refresh(sol)

        claude_response = '```json\n{"unit_price": 2.00, "quantity_wanted": 100}\n```'
        with patch(
            "app.services.excess_service._call_claude_bid_parse",
            new_callable=AsyncMock,
            return_value=claude_response,
        ):
            bid = await parse_bid_from_email(db_session, sol.id, "We bid $2 for 100 pcs")

        assert bid is not None
        assert float(bid.unit_price) == 2.00


# ---------------------------------------------------------------------------
# send_bid_solicitation
# ---------------------------------------------------------------------------


class TestSendBidSolicitation:
    @pytest.mark.asyncio
    async def test_bundled_mode(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item1 = _make_line_item(db_session, el, part_number="LM317T")
        item2 = _make_line_item(db_session, el, part_number="NE555P")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value=None)
        mock_gc.get_json = AsyncMock(
            return_value={"value": [{"id": "msg1", "conversationId": "conv1", "subject": "test"}]}
        )

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_bid_solicitation(
                db_session,
                list_id=el.id,
                line_item_ids=[item1.id, item2.id],
                recipient_email="buyer@test.com",
                recipient_name="John",
                contact_id=1,
                user_id=user.id,
                token="fake-token",
                bundled=True,
            )

        assert len(results) == 2
        assert all(s.status == "sent" for s in results)
        mock_gc.post_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_split_mode(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item1 = _make_line_item(db_session, el, part_number="LM317T")

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value=None)
        mock_gc.get_json = AsyncMock(return_value={"value": [{"id": "msg1", "subject": "test"}]})

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            results = await send_bid_solicitation(
                db_session,
                list_id=el.id,
                line_item_ids=[item1.id],
                recipient_email="buyer@test.com",
                recipient_name=None,
                contact_id=1,
                user_id=user.id,
                token="fake-token",
                bundled=False,
            )

        assert len(results) == 1
        assert results[0].status == "sent"

    @pytest.mark.asyncio
    async def test_bundled_mode_send_failure(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item1 = _make_line_item(db_session, el)

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=RuntimeError("Graph API error"))

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            results = await send_bid_solicitation(
                db_session,
                list_id=el.id,
                line_item_ids=[item1.id],
                recipient_email="buyer@test.com",
                recipient_name="John",
                contact_id=1,
                user_id=user.id,
                token="fake-token",
                bundled=True,
            )

        assert len(results) == 1
        assert results[0].status == "failed"

    @pytest.mark.asyncio
    async def test_split_mode_send_failure(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)
        item1 = _make_line_item(db_session, el)

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=RuntimeError("Graph API error"))

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            results = await send_bid_solicitation(
                db_session,
                list_id=el.id,
                line_item_ids=[item1.id],
                recipient_email="buyer@test.com",
                recipient_name=None,
                contact_id=1,
                user_id=user.id,
                token="fake-token",
                bundled=False,
            )

        assert len(results) == 1
        assert results[0].status == "failed"

    @pytest.mark.asyncio
    async def test_invalid_line_item_raises_404(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user)

        with pytest.raises(HTTPException) as exc_info:
            await send_bid_solicitation(
                db_session,
                list_id=el.id,
                line_item_ids=[99999],
                recipient_email="buyer@test.com",
                recipient_name=None,
                contact_id=1,
                user_id=user.id,
                token="fake-token",
                bundled=True,
            )
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# create_proactive_matches_for_excess
# ---------------------------------------------------------------------------


class TestCreateProactiveMatchesForExcess:
    def test_non_archived_status_returns_zero(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user, status="active")

        result = create_proactive_matches_for_excess(db_session, el.id, user_id=user.id)
        assert result["matches_created"] == 0

    def test_closed_status_processes(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user, status="closed")
        _make_line_item(db_session, el, part_number="NONEXISTENT123")

        result = create_proactive_matches_for_excess(db_session, el.id, user_id=user.id)
        # No matching requirements, so 0 matches
        assert result["matches_created"] == 0

    def test_expired_status_processes(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user, status="expired")
        _make_line_item(db_session, el, part_number="NONEXISTENT123")

        result = create_proactive_matches_for_excess(db_session, el.id, user_id=user.id)
        assert result["matches_created"] == 0

    def test_empty_part_number_skipped(self, db_session: Session):
        company = _make_company(db_session)
        user = _make_user(db_session)
        el = _make_excess_list(db_session, company, user, status="closed")
        # Create an item with empty part number
        item = ExcessLineItem(
            excess_list_id=el.id,
            part_number="---",  # normalizes to empty
            quantity=100,
        )
        db_session.add(item)
        db_session.commit()

        result = create_proactive_matches_for_excess(db_session, el.id, user_id=user.id)
        assert result["matches_created"] == 0

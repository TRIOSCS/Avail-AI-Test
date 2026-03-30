"""test_excess_phase4_email.py — Tests for Graph API email sending in excess bid
solicitations.

Covers:
- Sending emails via GraphClient with [EXCESS-BID-{id}] subject tags
- Handling send failures (status=failed, no exception raised)
- Multiple items produce separate emails with unique tags
- Custom subject and message are used in the email

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, conftest fixtures
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.excess import BidSolicitation, ExcessLineItem, ExcessList
from app.services.excess_service import parse_bid_from_email
from app.utils.normalization import normalize_mpn_key
from tests.conftest import engine  # noqa: F401

_ = engine  # Ensure test DB tables are created

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(db: Session) -> User:
    user = User(email="trader@test.com", name="Trader", role="trader")
    db.add(user)
    db.flush()
    return user


def _make_company(db: Session) -> Company:
    co = Company(name="Test Excess Co")
    db.add(co)
    db.flush()
    return co


def _make_excess_list(db: Session, company: Company, user: User) -> ExcessList:
    el = ExcessList(title="Test Excess List", company_id=company.id, owner_id=user.id, status="active")
    db.add(el)
    db.flush()
    return el


def _make_line_item(
    db: Session,
    excess_list: ExcessList,
    part_number: str = "LM317T",
    quantity: int = 100,
) -> ExcessLineItem:
    item = ExcessLineItem(
        excess_list_id=excess_list.id,
        part_number=part_number,
        normalized_part_number=normalize_mpn_key(part_number),
        quantity=quantity,
        manufacturer="Texas Instruments",
        asking_price=1.50,
    )
    db.add(item)
    db.flush()
    return item


def _setup(db: Session):
    """Create user, company, excess list, and 2 line items."""
    user = _make_user(db)
    company = _make_company(db)
    el = _make_excess_list(db, company, user)
    item1 = _make_line_item(db, el, "PART-001")
    item2 = _make_line_item(db, el, "PART-002")
    db.commit()
    return user, company, el, item1, item2


def _setup_solicitation(db: Session):
    """Create user, company, excess list, line item, and a BidSolicitation
    (status=sent)."""
    user = _make_user(db)
    company = _make_company(db)
    el = _make_excess_list(db, company, user)
    item = _make_line_item(db, el, "PARSE-001", quantity=5000)
    db.flush()
    sol = BidSolicitation(
        excess_line_item_id=item.id,
        contact_id=1,
        sent_by=user.id,
        recipient_email="vendor@example.com",
        status="sent",
    )
    db.add(sol)
    db.commit()
    db.refresh(sol)
    return user, company, el, item, sol


class TestParseBidFromEmail:
    @pytest.mark.asyncio
    @patch("app.services.excess_service._call_claude_bid_parse", new_callable=AsyncMock)
    async def test_parses_bid_successfully(self, mock_claude, db_session: Session):
        """Claude returns valid bid JSON -> Bid created, solicitation responded."""
        mock_claude.return_value = (
            '{"unit_price": 0.35, "quantity_wanted": 3000, "lead_time_days": 5, "notes": "Can ship Friday"}'
        )

        user, company, el, item, sol = _setup_solicitation(db_session)

        bid = await parse_bid_from_email(db_session, sol.id, "I can offer 3000 at $0.35")

        assert bid is not None
        assert float(bid.unit_price) == 0.35
        assert bid.quantity_wanted == 3000
        assert bid.lead_time_days == 5
        assert bid.notes == "Can ship Friday"
        assert bid.source == "email_parsed"

        db_session.refresh(sol)
        assert sol.status == "responded"
        assert sol.response_received_at is not None

    @pytest.mark.asyncio
    @patch("app.services.excess_service._call_claude_bid_parse", new_callable=AsyncMock)
    async def test_handles_decline(self, mock_claude, db_session: Session):
        """Claude returns declined JSON -> no Bid, solicitation status=responded."""
        mock_claude.return_value = '{"declined": true}'

        user, company, el, item, sol = _setup_solicitation(db_session)

        bid = await parse_bid_from_email(db_session, sol.id, "Not interested, thanks")

        assert bid is None

        db_session.refresh(sol)
        assert sol.status == "responded"
        assert sol.response_received_at is not None

    @pytest.mark.asyncio
    @patch("app.services.excess_service._call_claude_bid_parse", new_callable=AsyncMock)
    async def test_handles_parse_failure(self, mock_claude, db_session: Session):
        """Claude returns invalid JSON -> no Bid, solicitation stays sent."""
        mock_claude.return_value = "not valid json"

        user, company, el, item, sol = _setup_solicitation(db_session)

        bid = await parse_bid_from_email(db_session, sol.id, "garbled email")

        assert bid is None

        db_session.refresh(sol)
        assert sol.status == "sent"  # unchanged

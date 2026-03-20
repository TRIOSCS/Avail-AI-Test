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
from app.models.excess import ExcessLineItem, ExcessList
from app.services.excess_service import send_bid_solicitation
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGraphEmailSending:
    @pytest.mark.asyncio
    @patch("app.utils.graph_client.GraphClient")
    async def test_sends_email_via_graph(self, mock_gc_cls, db_session: Session):
        """GraphClient.post_json is called, graph_message_id stored, [EXCESS-BID-{id}]
        in subject."""
        mock_post = AsyncMock(return_value={"id": "graph-msg-123"})
        mock_gc_cls.return_value.post_json = mock_post

        user, company, el, item1, item2 = _setup(db_session)

        solicitations = await send_bid_solicitation(
            db_session,
            list_id=el.id,
            line_item_ids=[item1.id],
            recipient_email="buyer@example.com",
            recipient_name="Jane Buyer",
            contact_id=1,
            user_id=user.id,
            token="test-token",
        )

        assert len(solicitations) == 1
        s = solicitations[0]

        # Verify Graph API was called
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == "/me/sendMail"

        # Verify subject tag
        assert f"[EXCESS-BID-{s.id}]" in s.subject

        # Verify graph_message_id stored
        assert s.graph_message_id == "graph-msg-123"
        assert s.status == "sent"
        assert s.sent_at is not None

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.GraphClient")
    async def test_handles_send_failure(self, mock_gc_cls, db_session: Session):
        """GraphClient raises Exception -> solicitation status=failed, no exception
        raised."""
        mock_gc_cls.return_value.post_json = AsyncMock(side_effect=Exception("Graph API error"))

        user, company, el, item1, item2 = _setup(db_session)

        # Should NOT raise
        solicitations = await send_bid_solicitation(
            db_session,
            list_id=el.id,
            line_item_ids=[item1.id],
            recipient_email="buyer@example.com",
            recipient_name=None,
            contact_id=1,
            user_id=user.id,
            token="test-token",
        )

        assert len(solicitations) == 1
        assert solicitations[0].status == "failed"
        assert solicitations[0].graph_message_id is None
        assert solicitations[0].sent_at is None

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.GraphClient")
    async def test_multiple_items_send_separate_emails(self, mock_gc_cls, db_session: Session):
        """2 items -> 2 emails, 2 unique EXCESS-BID tags."""
        mock_post = AsyncMock(return_value={})
        mock_gc_cls.return_value.post_json = mock_post

        user, company, el, item1, item2 = _setup(db_session)

        solicitations = await send_bid_solicitation(
            db_session,
            list_id=el.id,
            line_item_ids=[item1.id, item2.id],
            recipient_email="buyer@example.com",
            recipient_name="John",
            contact_id=1,
            user_id=user.id,
            token="test-token",
        )

        assert len(solicitations) == 2
        assert mock_post.call_count == 2

        # Each solicitation has a unique EXCESS-BID tag
        tags = {s.subject.split("[EXCESS-BID-")[1].rstrip("]") for s in solicitations}
        assert len(tags) == 2  # unique IDs

        # Both are sent
        assert all(s.status == "sent" for s in solicitations)

    @pytest.mark.asyncio
    @patch("app.utils.graph_client.GraphClient")
    async def test_custom_subject_and_message(self, mock_gc_cls, db_session: Session):
        """User-provided subject and message are used in the email."""
        mock_post = AsyncMock(return_value={})
        mock_gc_cls.return_value.post_json = mock_post

        user, company, el, item1, item2 = _setup(db_session)

        solicitations = await send_bid_solicitation(
            db_session,
            list_id=el.id,
            line_item_ids=[item1.id],
            recipient_email="buyer@example.com",
            recipient_name="Jane",
            contact_id=1,
            user_id=user.id,
            token="test-token",
            subject="Special Offer for You",
            message="We have a great deal on these parts.",
        )

        assert len(solicitations) == 1
        s = solicitations[0]

        # Custom subject is used (with tag appended)
        assert "Special Offer for You" in s.subject
        assert f"[EXCESS-BID-{s.id}]" in s.subject

        # Custom message used as body preview
        assert s.body_preview == "We have a great deal on these parts."

        # Verify the email payload uses the custom subject and message
        call_payload = mock_post.call_args[0][1]
        assert "Special Offer for You" in call_payload["message"]["subject"]
        assert "great deal" in call_payload["message"]["body"]["content"]

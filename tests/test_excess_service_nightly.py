"""test_excess_service_nightly.py — Tests for send_bid_solicitation in excess_service.py.

Covers lines 868-997 (send_bid_solicitation bundled/split modes, invalid item, email
failure) and smaller missing blocks: 403, 508, 667, 675, 683.

Called by: pytest
Depends on: app/services/excess_service.py, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_company(db: Session, name: str = "Test Corp"):
    from app.models import Company

    co = Company(name=name)
    db.add(co)
    db.flush()
    return co


def _make_user(db: Session, email: str = "trader@nightly.com"):
    from app.models import User

    u = User(
        email=email,
        name="Trader",
        role="buyer",
        azure_id=email,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_excess_list(db: Session, company_id: int, owner_id: int, title: str = "Test Excess"):
    from app.models.excess import ExcessList

    el = ExcessList(company_id=company_id, owner_id=owner_id, title=title, status="active")
    db.add(el)
    db.flush()
    return el


def _make_line_item(db: Session, excess_list_id: int, part_number: str, qty: int = 100):
    from app.models.excess import ExcessLineItem

    item = ExcessLineItem(
        excess_list_id=excess_list_id,
        part_number=part_number,
        quantity=qty,
        asking_price=None,
    )
    db.add(item)
    db.flush()
    return item


# ── send_bid_solicitation: bundled mode ──────────────────────────────────────


@pytest.mark.asyncio
async def test_send_bid_solicitation_bundled_creates_sent_records(db_session: Session):
    """Bundled mode: one email sent, all BidSolicitations set to SENT with graph_message_id."""
    from app.constants import BidSolicitationStatus
    from app.models.excess import BidSolicitation
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session)
    user = _make_user(db_session)
    excess_list = _make_excess_list(db_session, co.id, user.id, title="Bundled List")
    item1 = _make_line_item(db_session, excess_list.id, "LM317T", qty=100)
    item2 = _make_line_item(db_session, excess_list.id, "NE555", qty=200)
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = {"id": "graph-msg-123"}

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item1.id, item2.id],
                recipient_email="buyer@test.com",
                recipient_name="Test Buyer",
                contact_id=1,
                user_id=user.id,
                token="fake-token",
                bundled=True,
            )

    assert len(result) == 2
    for s in result:
        assert s.status == BidSolicitationStatus.SENT
        assert s.graph_message_id == "graph-msg-123"
        assert s.sent_at is not None
        assert s.recipient_email == "buyer@test.com"

    # Verify both solicitations share the same email subject
    subjects = {s.subject for s in result}
    assert len(subjects) == 1
    assert "EXCESS-BID" in subjects.pop()

    # Confirm records persisted in DB
    db_rows = db_session.query(BidSolicitation).all()
    assert len(db_rows) == 2


@pytest.mark.asyncio
async def test_send_bid_solicitation_bundled_single_item(db_session: Session):
    """Bundled mode with one item: one solicitation created and marked SENT."""
    from app.constants import BidSolicitationStatus
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "SingleCo")
    user = _make_user(db_session, "single@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "STM32F103")
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = {"id": "msg-abc"}

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item.id],
                recipient_email="vendor@supply.com",
                recipient_name="Vendor Joe",
                contact_id=2,
                user_id=user.id,
                token="tok",
                bundled=True,
            )

    assert len(result) == 1
    assert result[0].status == BidSolicitationStatus.SENT
    assert result[0].graph_message_id == "msg-abc"
    # ONE email was sent (one post_json call)
    gc_instance.post_json.assert_called_once()


@pytest.mark.asyncio
async def test_send_bid_solicitation_bundled_no_message_id_when_find_returns_none(
    db_session: Session,
):
    """Bundled mode: if _find_sent_message returns None, graph_message_id is None but status is SENT."""
    from app.constants import BidSolicitationStatus
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "NullMsgCo")
    user = _make_user(db_session, "nullmsg@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "TL431")
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = None

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item.id],
                recipient_email="x@y.com",
                recipient_name=None,
                contact_id=3,
                user_id=user.id,
                token="tok2",
                bundled=True,
            )

    assert result[0].status == BidSolicitationStatus.SENT
    assert result[0].graph_message_id is None


# ── send_bid_solicitation: split mode ────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_bid_solicitation_split_creates_one_solicitation_per_item(
    db_session: Session,
):
    """Split mode: separate email per item, each BidSolicitation gets its own subject."""
    from app.constants import BidSolicitationStatus
    from app.models.excess import BidSolicitation
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "SplitCo")
    user = _make_user(db_session, "split@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id, title="Split List")
    item1 = _make_line_item(db_session, excess_list.id, "BC547", qty=500)
    item2 = _make_line_item(db_session, excess_list.id, "BC557", qty=300)
    item3 = _make_line_item(db_session, excess_list.id, "2N3904", qty=1000)
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = {"id": "split-msg-id"}

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item1.id, item2.id, item3.id],
                recipient_email="buyer@split.com",
                recipient_name="Split Buyer",
                contact_id=4,
                user_id=user.id,
                token="split-token",
                bundled=False,
            )

    # One BidSolicitation per item
    assert len(result) == 3
    for s in result:
        assert s.status == BidSolicitationStatus.SENT
        assert s.graph_message_id == "split-msg-id"
        assert s.sent_at is not None
        assert "EXCESS-BID" in (s.subject or "")

    # Subjects should each reference different solicitation IDs
    subjects = [s.subject for s in result]
    assert len(set(subjects)) == 3

    # post_json called once per item
    assert gc_instance.post_json.call_count == 3

    # All persisted
    db_rows = db_session.query(BidSolicitation).all()
    assert len(db_rows) == 3


@pytest.mark.asyncio
async def test_send_bid_solicitation_split_uses_part_number_in_subject(db_session: Session):
    """Split mode: auto-generated subject includes part_number and quantity."""
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "SubjectCo")
    user = _make_user(db_session, "subject@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id, title="MPN List")
    item = _make_line_item(db_session, excess_list.id, "LM741", qty=250)
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = {"id": "m1"}

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item.id],
                recipient_email="v@vendor.com",
                recipient_name=None,
                contact_id=5,
                user_id=user.id,
                token="tok3",
                bundled=False,
            )

    subject = result[0].subject or ""
    assert "LM741" in subject
    assert "250" in subject


@pytest.mark.asyncio
async def test_send_bid_solicitation_split_no_find_message_id(db_session: Session):
    """Split mode: if _find_sent_message returns None, graph_message_id is None but SENT."""
    from app.constants import BidSolicitationStatus
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "SplitNullMsgCo")
    user = _make_user(db_session, "splitnull@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "IRF540N", qty=50)
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = None

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item.id],
                recipient_email="z@zz.com",
                recipient_name=None,
                contact_id=6,
                user_id=user.id,
                token="tok4",
                bundled=False,
            )

    assert result[0].status == BidSolicitationStatus.SENT
    assert result[0].graph_message_id is None


# ── send_bid_solicitation: invalid line item (line 867) ──────────────────────


@pytest.mark.asyncio
async def test_send_bid_solicitation_invalid_item_not_in_list_raises_404(db_session: Session):
    """Line item belonging to a different list causes 404 before any email is sent."""
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "WrongListCo")
    user = _make_user(db_session, "wronglist@nightly.com")

    # Two separate lists
    list_a = _make_excess_list(db_session, co.id, user.id, title="List A")
    list_b = _make_excess_list(db_session, co.id, user.id, title="List B")
    item_on_b = _make_line_item(db_session, list_b.id, "WRONG_PART", qty=10)
    db_session.commit()

    gc_instance = AsyncMock()
    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with pytest.raises(HTTPException) as exc_info:
            await send_bid_solicitation(
                db_session,
                list_id=list_a.id,
                line_item_ids=[item_on_b.id],
                recipient_email="x@y.com",
                recipient_name=None,
                contact_id=1,
                user_id=user.id,
                token="tok",
                bundled=True,
            )

    assert exc_info.value.status_code == 404
    # No email should have been sent
    gc_instance.post_json.assert_not_called()


@pytest.mark.asyncio
async def test_send_bid_solicitation_nonexistent_item_raises_404(db_session: Session):
    """Completely nonexistent line_item_id raises 404."""
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "NonExistCo")
    user = _make_user(db_session, "nonexist@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    db_session.commit()

    gc_instance = AsyncMock()
    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with pytest.raises(HTTPException) as exc_info:
            await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[99999],
                recipient_email="x@y.com",
                recipient_name=None,
                contact_id=1,
                user_id=user.id,
                token="tok",
                bundled=False,
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_send_bid_solicitation_second_item_invalid_raises_404(db_session: Session):
    """Validation loop: first item valid, second item invalid → 404 before email send."""
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "MixedCo")
    user = _make_user(db_session, "mixed@nightly.com")
    list_a = _make_excess_list(db_session, co.id, user.id, title="Mixed")
    item_valid = _make_line_item(db_session, list_a.id, "VALID_PART", qty=10)
    db_session.commit()

    gc_instance = AsyncMock()
    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with pytest.raises(HTTPException) as exc_info:
            await send_bid_solicitation(
                db_session,
                list_id=list_a.id,
                line_item_ids=[item_valid.id, 88888],
                recipient_email="a@b.com",
                recipient_name=None,
                contact_id=1,
                user_id=user.id,
                token="tok",
                bundled=True,
            )

    assert exc_info.value.status_code == 404
    gc_instance.post_json.assert_not_called()


# ── send_bid_solicitation: email failure (lines 923-930 and 974-981) ─────────


@pytest.mark.asyncio
async def test_send_bid_solicitation_bundled_email_failure_marks_failed(db_session: Session):
    """Bundled mode: Graph API exception sets all solicitations to FAILED."""
    from app.constants import BidSolicitationStatus
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "FailCo")
    user = _make_user(db_session, "fail@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item1 = _make_line_item(db_session, excess_list.id, "ATmega328P", qty=50)
    item2 = _make_line_item(db_session, excess_list.id, "ATmega2560", qty=25)
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(side_effect=Exception("Graph API timeout"))

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        result = await send_bid_solicitation(
            db_session,
            list_id=excess_list.id,
            line_item_ids=[item1.id, item2.id],
            recipient_email="fail@buyer.com",
            recipient_name="Fail Buyer",
            contact_id=7,
            user_id=user.id,
            token="bad-token",
            bundled=True,
        )

    assert len(result) == 2
    for s in result:
        assert s.status == BidSolicitationStatus.FAILED


@pytest.mark.asyncio
async def test_send_bid_solicitation_split_email_failure_marks_item_failed(db_session: Session):
    """Split mode: Graph API exception for one item sets that solicitation to FAILED."""
    from app.constants import BidSolicitationStatus
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "SplitFailCo")
    user = _make_user(db_session, "splitfail@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "MOSFET", qty=75)
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(side_effect=RuntimeError("SMTP connection refused"))

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        result = await send_bid_solicitation(
            db_session,
            list_id=excess_list.id,
            line_item_ids=[item.id],
            recipient_email="fail2@buyer.com",
            recipient_name=None,
            contact_id=8,
            user_id=user.id,
            token="bad-token2",
            bundled=False,
        )

    assert len(result) == 1
    assert result[0].status == BidSolicitationStatus.FAILED


@pytest.mark.asyncio
async def test_send_bid_solicitation_split_partial_failure(db_session: Session):
    """Split mode: first item succeeds, second fails — each gets correct status."""
    from app.constants import BidSolicitationStatus
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "PartialFailCo")
    user = _make_user(db_session, "partfail@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item1 = _make_line_item(db_session, excess_list.id, "PART_OK", qty=100)
    item2 = _make_line_item(db_session, excess_list.id, "PART_BAD", qty=50)
    db_session.commit()

    # First call succeeds, second raises
    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(
        side_effect=[None, Exception("Rate limit exceeded")]
    )

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = {"id": "ok-msg"}

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item1.id, item2.id],
                recipient_email="partial@buyer.com",
                recipient_name="Partial Buyer",
                contact_id=9,
                user_id=user.id,
                token="partial-token",
                bundled=False,
            )

    assert len(result) == 2
    assert result[0].status == BidSolicitationStatus.SENT
    assert result[1].status == BidSolicitationStatus.FAILED


# ── send_bid_solicitation: custom subject/message ────────────────────────────


@pytest.mark.asyncio
async def test_send_bid_solicitation_bundled_custom_subject_and_message(db_session: Session):
    """Bundled mode: custom subject and message are used in the email."""
    from app.constants import BidSolicitationStatus
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "CustomCo")
    user = _make_user(db_session, "custom@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "CUSTOM_PART", qty=10)
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(return_value=None)

    custom_subject = "Custom Bid Subject"
    custom_message = "Please reply with your very best price."

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = {"id": "custom-msg"}

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item.id],
                recipient_email="custom@vendor.com",
                recipient_name="Custom Vendor",
                contact_id=10,
                user_id=user.id,
                token="custom-token",
                subject=custom_subject,
                message=custom_message,
                bundled=True,
            )

    assert result[0].status == BidSolicitationStatus.SENT
    # Custom subject is embedded in the tagged subject
    assert custom_subject in result[0].subject
    # Custom message is stored in body_preview
    assert custom_message in result[0].body_preview


@pytest.mark.asyncio
async def test_send_bid_solicitation_split_custom_subject(db_session: Session):
    """Split mode: custom subject is used (with EXCESS-BID tag appended)."""
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "SplitCustomCo")
    user = _make_user(db_session, "splitcustom@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "SPLIT_CUSTOM", qty=10)
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = {"id": "sc-msg"}

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item.id],
                recipient_email="sc@vendor.com",
                recipient_name=None,
                contact_id=11,
                user_id=user.id,
                token="sc-token",
                subject="My Custom Subject",
                bundled=False,
            )

    assert "My Custom Subject" in result[0].subject
    assert "EXCESS-BID" in result[0].subject


# ── send_bid_solicitation: recipient_name=None (no greeting prefix) ──────────


@pytest.mark.asyncio
async def test_send_bid_solicitation_bundled_no_recipient_name(db_session: Session):
    """Bundled mode: recipient_name=None is accepted (stored as None)."""
    from app.constants import BidSolicitationStatus
    from app.services.excess_service import send_bid_solicitation

    co = _make_company(db_session, "NoNameCo")
    user = _make_user(db_session, "noname@nightly.com")
    excess_list = _make_excess_list(db_session, co.id, user.id)
    item = _make_line_item(db_session, excess_list.id, "NO_NAME_PART", qty=5)
    db_session.commit()

    gc_instance = AsyncMock()
    gc_instance.post_json = AsyncMock(return_value=None)

    with patch("app.utils.graph_client.GraphClient", return_value=gc_instance):
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
        ) as mock_find:
            mock_find.return_value = {"id": "nn-msg"}

            result = await send_bid_solicitation(
                db_session,
                list_id=excess_list.id,
                line_item_ids=[item.id],
                recipient_email="noname@vendor.com",
                recipient_name=None,
                contact_id=12,
                user_id=user.id,
                token="nn-token",
                bundled=True,
            )

    assert result[0].status == BidSolicitationStatus.SENT
    assert result[0].recipient_name is None

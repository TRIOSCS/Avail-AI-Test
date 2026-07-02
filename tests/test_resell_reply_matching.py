"""Tests for the resell reply-matching tier wired into email_service.poll_inbox (RS-4).

The send path stamps ``ExcessOutreach.graph_conversation_id`` / ``graph_message_id``; this
covers the INBOUND half — poll_inbox's new Tier-2.5 matches a buyer's reply to those rows
(reusing resell_outreach_service._match_outreach), advances the outreach via
record_response(commit=False) inside the per-message savepoint, and logs the reply on the
resell timeline. Also covers the tier ordering (a Contact match preempts resell), cross-poll
idempotency, and the purely-resell AI-parse skip.

Graph is mocked at the source (GraphClient) — no network. Called by: pytest.
Depends on: app.email_service, app.services.resell_outreach_service, tests.conftest.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.email_service import poll_inbox
from app.models import ActivityLog, Company, Contact, ExcessList, ExcessOutreach, User, VendorCard, VendorResponse
from tests.conftest import engine

_ = engine


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def owner(db_session: Session) -> User:
    u = User(email="rs4-owner@trioscs.com", name="RS4 Owner", role="trader", azure_id="rs4-owner")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def buyer_card(db_session: Session) -> VendorCard:
    vc = VendorCard(normalized_name="buyer one", display_name="Buyer One", emails=["sales@buyerone.com"])
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


@pytest.fixture()
def excess_list(db_session: Session, owner: User) -> ExcessList:
    co = Company(name="Seller Co")
    db_session.add(co)
    db_session.flush()
    el = ExcessList(company_id=co.id, owner_id=owner.id, title="RS4 Excess", status="open")
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


def _outreach(db, el, card, owner, conv="conv-rs4", msg="msg-rs4", status="sent") -> ExcessOutreach:
    row = ExcessOutreach(
        excess_list_id=el.id,
        target_vendor_card_id=card.id,
        submitted_by=owner.id,
        channel="email",
        status=status,
        graph_conversation_id=conv,
        graph_message_id=msg,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _inbox_message(msg_id="msg-rs4", conv_id="conv-rs4", sender="sales@buyerone.com"):
    return {
        "id": msg_id,
        "subject": "RE: your excess offer",
        "from": {"emailAddress": {"address": sender, "name": "Buyer One"}},
        "bodyPreview": "We'll take 500 at $1.25",
        "body": {"content": "<p>We'll take 500 at $1.25</p>"},
        "conversationId": conv_id,
        "receivedDateTime": None,
    }


async def _run_poll(db, messages, *, credential=None):
    mock_gc = AsyncMock()
    mock_gc.get_json.return_value = {"value": messages}
    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.email_service.get_credential_cached", return_value=credential),
        patch("app.email_service._submit_parse_batch", new_callable=AsyncMock) as submit_batch,
    ):
        results = await poll_inbox(token="fake-token", db=db)
    return results, submit_batch


# ── Tests ────────────────────────────────────────────────────────────


class TestResellReplyMatching:
    @pytest.mark.asyncio
    async def test_match_by_conversation_id_advances_and_logs(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, owner: User
    ):
        row = _outreach(db_session, excess_list, buyer_card, owner)

        await _run_poll(db_session, [_inbox_message()])

        db_session.refresh(row)
        assert row.status == "responded"

        vr = db_session.query(VendorResponse).filter(VendorResponse.message_id == "msg-rs4").one()
        assert vr.status == "matched"

        # The inbound reply lands on the resell timeline, scoped to the list + buyer.
        act = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.excess_list_id == excess_list.id, ActivityLog.direction == "inbound")
            .all()
        )
        assert len(act) == 1
        assert act[0].vendor_card_id == buyer_card.id
        assert act[0].external_id == "msg-rs4"

    @pytest.mark.asyncio
    async def test_auto_reply_does_not_advance_or_stop_clock(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, owner: User
    ):
        """An OOO/bounce auto-reply matches the outreach thread but must NOT advance it
        to 'responded' or log a meaningful inbound reply — doing so would stop the
        follow-up clock on a buyer who never actually replied.

        The VendorResponse still records the raw inbound message.
        """
        row = _outreach(db_session, excess_list, buyer_card, owner)
        msg = _inbox_message()
        msg["subject"] = "Automatic reply: Out of Office"
        msg["body"] = {"content": "I am currently out of the office and will return Monday."}
        msg["bodyPreview"] = "I am currently out of the office"

        await _run_poll(db_session, [msg])

        db_session.refresh(row)
        assert row.status == "sent"  # NOT advanced by an auto-reply

        # The raw inbound message is still recorded (matched)...
        vr = db_session.query(VendorResponse).filter(VendorResponse.message_id == "msg-rs4").one()
        assert vr.status == "matched"
        # ...but no inbound resell activity log fires (which would stop the follow-up clock).
        act = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.excess_list_id == excess_list.id, ActivityLog.direction == "inbound")
            .count()
        )
        assert act == 0

    @pytest.mark.asyncio
    async def test_match_by_message_id_fallback(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, owner: User
    ):
        # No conversation id on the row — only the message id was captured at send time.
        row = _outreach(db_session, excess_list, buyer_card, owner, conv=None, msg="msg-only")
        # Incoming message carries a conversation id that matches NOTHING → message-id fallback.
        await _run_poll(db_session, [_inbox_message(msg_id="msg-only", conv_id="unrelated-conv")])

        db_session.refresh(row)
        assert row.status == "responded"

    @pytest.mark.asyncio
    async def test_contact_match_preempts_resell(
        self,
        db_session: Session,
        excess_list: ExcessList,
        buyer_card: VendorCard,
        owner: User,
        test_user: User,
        test_requisition,
    ):
        # An RFQ Contact on the SAME conversation (Tier-1 exact) must win; the outreach on
        # that conversation is never treated as a resell reply.
        row = _outreach(db_session, excess_list, buyer_card, owner, conv="conv-shared", msg="msg-shared")
        contact = Contact(
            requisition_id=test_requisition.id,
            user_id=test_user.id,
            contact_type="email",
            vendor_name="Buyer One",
            vendor_contact="sales@buyerone.com",
            graph_conversation_id="conv-shared",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(contact)
        db_session.commit()

        await _run_poll(db_session, [_inbox_message(msg_id="msg-shared", conv_id="conv-shared")])

        db_session.refresh(row)
        assert row.status == "sent"  # untouched — the reply was a Contact (RFQ) match

        vr = db_session.query(VendorResponse).filter(VendorResponse.message_id == "msg-shared").one()
        assert vr.contact_id == contact.id
        # No resell-scoped inbound activity was written.
        assert db_session.query(ActivityLog).filter(ActivityLog.excess_list_id == excess_list.id).count() == 0

    @pytest.mark.asyncio
    async def test_idempotent_across_two_polls(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, owner: User
    ):
        row = _outreach(db_session, excess_list, buyer_card, owner)

        await _run_poll(db_session, [_inbox_message()])
        await _run_poll(db_session, [_inbox_message()])  # same message again

        db_session.refresh(row)
        assert row.status == "responded"
        # The message is de-duped by already_processed on the 2nd poll — no dup VR/activity.
        assert db_session.query(VendorResponse).filter(VendorResponse.message_id == "msg-rs4").count() == 1
        assert db_session.query(ActivityLog).filter(ActivityLog.excess_list_id == excess_list.id).count() == 1

    @pytest.mark.asyncio
    async def test_purely_resell_reply_skips_ai_parse(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, owner: User
    ):
        _outreach(db_session, excess_list, buyer_card, owner)

        # Credential PRESENT — the only reason parse is skipped is the purely-resell gate.
        _results, submit_batch = await _run_poll(db_session, [_inbox_message()], credential="sk-key")

        submit_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_per_line_campaign_logs_reply_once(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, owner: User
    ):
        # Two outreach rows share one conversation (a per-line campaign). One reply must
        # advance BOTH but log the inbound activity only ONCE.
        r1 = _outreach(db_session, excess_list, buyer_card, owner, msg="msg-a")
        r2 = ExcessOutreach(
            excess_list_id=excess_list.id,
            target_vendor_card_id=buyer_card.id,
            submitted_by=owner.id,
            channel="email",
            status="sent",
            graph_conversation_id="conv-rs4",
            graph_message_id="msg-b",
        )
        db_session.add(r2)
        db_session.commit()

        await _run_poll(db_session, [_inbox_message()])

        db_session.refresh(r1)
        db_session.refresh(r2)
        assert r1.status == "responded"
        assert r2.status == "responded"
        assert db_session.query(ActivityLog).filter(ActivityLog.excess_list_id == excess_list.id).count() == 1

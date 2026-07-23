"""Tests for resell_outreach_service (Chunk B) — send/log + reply adapter.

Covers submit_outreach (email path stamps graph ids via the send_batch_rfq adapter;
manual-log path writes rows only; self/non-owner guards; counterparty canonicalization),
record_response (reply advances status + links an inbound ExcessOffer), and the
counterparty_card canonicalizer (company-only → backfilled VendorCard).

Graph / send_batch_rfq are mocked AT THE SOURCE (GraphClient + send_batch_rfq /
_find_sent_message in app.email_service) so no network is touched and the live RFQ
path is never exercised destructively.

Called by: pytest
Depends on: app.services.resell_outreach_service, tests.conftest
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOutreachStatus
from app.models import Company, ExcessList, ExcessOutreach, User, VendorCard
from app.models.excess import ExcessLineItem, ExcessOffer
from app.services import resell_outreach_service as svc
from tests.conftest import engine

_ = engine


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def seller_company(db_session: Session) -> Company:
    co = Company(name="Acme Corp")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def trader(db_session: Session) -> User:
    u = User(
        email="b-trader@trioscs.com",
        name="B Trader",
        role="trader",
        azure_id="b-trader-001",
        m365_connected=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def other_user(db_session: Session) -> User:
    u = User(
        email="b-other@trioscs.com",
        name="B Other",
        role="trader",
        azure_id="b-other-001",
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def buyer_card(db_session: Session) -> VendorCard:
    vc = VendorCard(
        normalized_name="buyer one",
        display_name="Buyer One",
        emails=["sales@buyerone.com"],
    )
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


@pytest.fixture()
def buyer_company(db_session: Session) -> Company:
    co = Company(name="Globex Trading", domain="globex.com")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def excess_list(db_session: Session, seller_company: Company, trader: User) -> ExcessList:
    """A POSTED (open) list — ``_guard_owner`` rejects a DRAFT list (finding #47), and
    outreach is only ever offered out on a posted posting."""
    el = ExcessList(company_id=seller_company.id, owner_id=trader.id, title="Q1 Excess", status=ExcessListStatus.OPEN)
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def line_item(db_session: Session, excess_list: ExcessList) -> ExcessLineItem:
    li = ExcessLineItem(excess_list_id=excess_list.id, part_number="LM358N", quantity=500)
    db_session.add(li)
    db_session.commit()
    db_session.refresh(li)
    return li


# ── counterparty_card canonicalizer ──────────────────────────────────


class TestCounterpartyCard:
    def test_passthrough_vendor_card(self, db_session: Session, buyer_card: VendorCard):
        card = svc.counterparty_card(db_session, vendor_card_id=buyer_card.id)
        assert card.id == buyer_card.id

    def test_company_only_backfills_card(self, db_session: Session, buyer_company: Company):
        card = svc.counterparty_card(db_session, company_id=buyer_company.id)
        assert card is not None
        assert card.id is not None
        # Canonicalized on the shared normalized_name key + carried domain.
        assert card.normalized_name == buyer_company.normalized_name
        assert card.display_name == "Globex Trading"
        assert card.domain == "globex.com"

    def test_company_only_matches_existing_card(self, db_session: Session, buyer_company: Company):
        # A pre-existing card on the same normalized key must be REUSED, not duplicated.
        pre = VendorCard(normalized_name=buyer_company.normalized_name, display_name="Globex")
        db_session.add(pre)
        db_session.commit()
        card = svc.counterparty_card(db_session, company_id=buyer_company.id)
        assert card.id == pre.id

    def test_requires_one_identifier(self, db_session: Session):
        with pytest.raises(ValueError):
            svc.counterparty_card(db_session)


# ── submit_outreach: guards ──────────────────────────────────────────


class TestSubmitOutreachGuards:
    def test_non_owner_blocked(
        self, db_session: Session, excess_list: ExcessList, other_user: User, buyer_card: VendorCard
    ):
        with pytest.raises(HTTPException) as exc:
            svc.submit_outreach(
                db_session,
                list_id=excess_list.id,
                owner=other_user,
                buyers=[{"vendor_card_id": buyer_card.id}],
                scope="whole_list",
                channel="phone",
                send_email=False,
            )
        assert exc.value.status_code == 403

    def test_cannot_post_role_blocked(self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard):
        # The list owner is a trader, but a buyer-role user (even as owner) lacks can_post.
        buyer_owner = User(email="bo@trioscs.com", name="BO", role="buyer", azure_id="bo-1")
        db_session.add(buyer_owner)
        db_session.commit()
        excess_list.owner_id = buyer_owner.id
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            svc.submit_outreach(
                db_session,
                list_id=excess_list.id,
                owner=buyer_owner,
                buyers=[{"vendor_card_id": buyer_card.id}],
                scope="whole_list",
                channel="phone",
                send_email=False,
            )
        assert exc.value.status_code == 403

    def test_draft_list_blocked(
        self, db_session: Session, excess_list: ExcessList, trader: User, buyer_card: VendorCard
    ):
        """Finding #47: ``_guard_owner`` rejects a DRAFT list at the SERVICE layer too —
        the router already 409s a draft outreach submit, but a direct service call must
        not be able to attach outreach to a private, never-posted list."""
        excess_list.status = ExcessListStatus.DRAFT
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            svc.submit_outreach(
                db_session,
                list_id=excess_list.id,
                owner=trader,
                buyers=[{"vendor_card_id": buyer_card.id}],
                scope="whole_list",
                channel="phone",
                send_email=False,
            )
        assert exc.value.status_code == 409


# ── submit_outreach: manual-log path ─────────────────────────────────


class TestSubmitOutreachManualLog:
    def test_whole_list_one_row_per_buyer(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        outreach = svc.submit_outreach(
            db_session,
            list_id=excess_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            channel="phone",
            send_email=False,
            notes="called them",
        )
        assert len(outreach) == 1
        row = outreach[0]
        assert row.channel == "phone"
        assert row.status == "sent"
        assert row.target_vendor_card_id == buyer_card.id
        assert row.excess_line_item_id is None  # whole-list → no specific line
        assert row.submitted_by == trader.id
        # No email → no graph ids stamped.
        assert row.graph_message_id is None
        assert row.graph_conversation_id is None

    def test_per_line_one_row_per_buyer_x_line(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        outreach = svc.submit_outreach(
            db_session,
            list_id=excess_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="per_line",
            channel="marketplace",
            send_email=False,
        )
        assert len(outreach) == 1
        assert outreach[0].excess_line_item_id == line_item.id
        assert outreach[0].channel == "marketplace"

    def test_manual_log_writes_activity(
        self,
        db_session: Session,
        excess_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        from app.models import ActivityLog

        svc.submit_outreach(
            db_session,
            list_id=excess_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            channel="phone",
            send_email=False,
            notes="left a voicemail re: surplus LM358N",
        )
        logs = db_session.query(ActivityLog).filter(ActivityLog.excess_list_id == excess_list.id).all()
        assert len(logs) == 1
        assert logs[0].vendor_card_id == buyer_card.id
        assert logs[0].direction == "outbound"
        # Item-0 (Chunk B carry-over): the documented ``notes`` param must be written
        # to ActivityLog.notes, never silently dropped.
        assert logs[0].notes == "left a voicemail re: surplus LM358N"

    def test_company_only_buyer_backfills_card(
        self,
        db_session: Session,
        excess_list: ExcessList,
        trader: User,
        buyer_company: Company,
    ):
        outreach = svc.submit_outreach(
            db_session,
            list_id=excess_list.id,
            owner=trader,
            buyers=[{"company_id": buyer_company.id}],
            scope="whole_list",
            channel="phone",
            send_email=False,
        )
        assert len(outreach) == 1
        card = db_session.get(VendorCard, outreach[0].target_vendor_card_id)
        assert card is not None
        assert card.normalized_name == buyer_company.normalized_name


# ── submit_outreach: email path (send_batch_rfq adapter) ─────────────


class TestSubmitOutreachEmail:
    @pytest.mark.asyncio
    async def test_email_path_stamps_graph_ids(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        # send_batch_rfq is mocked at the source: it reports the send succeeded.
        async def _fake_send(*_args, **_kwargs):
            return [
                {
                    "vendor_name": "Buyer One",
                    "vendor_email": "sales@buyerone.com",
                    "status": "sent",
                }
            ]

        # _find_sent_message (the graph-id lookup send_batch_rfq itself uses) is mocked
        # at the source to return the just-sent message's ids.
        async def _fake_lookup(_gc, _subject, _email):
            return {"id": "msg-graph-1", "conversationId": "conv-graph-1"}

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_fake_send),
            patch("app.email_service._find_sent_message", side_effect=_fake_lookup),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            outreach = await svc.submit_outreach_email(
                db_session,
                list_id=excess_list.id,
                owner=trader,
                buyers=[{"vendor_card_id": buyer_card.id, "email": "sales@buyerone.com"}],
                scope="whole_list",
                token="fake-token",
                subject="Excess available",
                body="We have surplus stock you may want.",
            )

        assert len(outreach) == 1
        row = outreach[0]
        assert row.channel == "email"
        assert row.status == "sent"
        assert row.graph_message_id == "msg-graph-1"
        assert row.graph_conversation_id == "conv-graph-1"

    @pytest.mark.asyncio
    async def test_email_skipped_recipient_flagged_not_dropped(
        self,
        db_session: Session,
        excess_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        # send_batch_rfq reports the recipient was skipped (DNC / no email): the row must
        # still exist, flagged FAILED with the skip reason persisted — never silently
        # dropped, and never NO_RESPONSE (a send that never went out is not buyer silence).
        async def _fake_send(*_args, **_kwargs):
            return [
                {
                    "vendor_name": "Buyer One",
                    "vendor_email": "sales@buyerone.com",
                    "status": "skipped",
                    "error": "do-not-contact",
                }
            ]

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_fake_send),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            outreach = await svc.submit_outreach_email(
                db_session,
                list_id=excess_list.id,
                owner=trader,
                buyers=[{"vendor_card_id": buyer_card.id, "email": "sales@buyerone.com"}],
                scope="whole_list",
                token="fake-token",
                subject="Excess available",
                body="surplus",
            )
        assert len(outreach) == 1
        assert outreach[0].status == ExcessOutreachStatus.FAILED
        assert outreach[0].send_error == "do-not-contact"
        assert outreach[0].graph_message_id is None

    @pytest.mark.asyncio
    async def test_dnc_buyer_skipped_end_to_end_not_emailed(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        """S3.3: a real DNC SiteContact on the buyer's email is skipped at send time.

        Unlike test_email_skipped_recipient_flagged_not_dropped (which stubs
        send_batch_rfq), this exercises the REAL send_batch_rfq DNC query: a do-not-
        contact SiteContact whose email matches the buyer card means the recipient is
        never passed to GraphClient.post_json, and the row is recorded FAILED with the
        skip reason (never NO_RESPONSE). Proves the resell vendor-send path inherits the
        DNC block.
        """
        from app.models.crm import CustomerSite, SiteContact

        site = CustomerSite(company_id=excess_list.company_id, site_name="Buyer Site", is_active=True)
        db_session.add(site)
        db_session.flush()
        db_session.add(
            SiteContact(
                customer_site_id=site.id,
                full_name="Blocked Buyer",
                email="sales@buyerone.com",  # matches buyer_card.emails
                do_not_contact=True,
            )
        )
        db_session.commit()

        mock_gc = AsyncMock()
        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.email_service.get_credential_cached", return_value=None),
        ):
            outreach = await svc.submit_outreach_email(
                db_session,
                list_id=excess_list.id,
                owner=trader,
                buyers=[{"vendor_card_id": buyer_card.id, "email": "sales@buyerone.com"}],
                scope="whole_list",
                token="fake-token",
                subject="Excess available",
                body="surplus",
            )

        assert len(outreach) == 1
        assert outreach[0].status == ExcessOutreachStatus.FAILED
        assert outreach[0].send_error == "do-not-contact"
        # The DNC recipient must never reach Graph sendMail.
        mock_gc.post_json.assert_not_called()


# ── record_response: reply adapter ───────────────────────────────────


class TestRecordResponse:
    def _make_outreach(self, db, excess_list, buyer_card, trader, conv="conv-1", msg="msg-1"):
        o = ExcessOutreach(
            excess_list_id=excess_list.id,
            target_vendor_card_id=buyer_card.id,
            submitted_by=trader.id,
            channel="email",
            status="sent",
            graph_message_id=msg,
            graph_conversation_id=conv,
        )
        db.add(o)
        db.commit()
        db.refresh(o)
        return o

    def test_reply_without_offer_advances_to_responded(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, trader: User
    ):
        o = self._make_outreach(db_session, excess_list, buyer_card, trader)
        updated = svc.record_response(
            db_session,
            conversation_id="conv-1",
            has_offer=False,
        )
        assert len(updated) == 1
        assert updated[0].id == o.id
        assert updated[0].status == "responded"

    def test_reply_with_offer_advances_to_bid_and_links_offer(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        buyer_card: VendorCard,
        trader: User,
    ):
        o = self._make_outreach(db_session, excess_list, buyer_card, trader)
        updated = svc.record_response(
            db_session,
            conversation_id="conv-1",
            has_offer=True,
            offer_lines=[{"mpn_raw": "LM358N", "quantity": 500, "unit_price": "1.25"}],
        )
        assert updated[0].status == "bid"
        # An inbound ExcessOffer was created, scoped to the canonical buyer vendor card.
        offers = db_session.query(ExcessOffer).filter(ExcessOffer.excess_list_id == excess_list.id).all()
        assert len(offers) == 1
        assert offers[0].offerer_vendor_card_id == buyer_card.id
        assert offers[0].status == "open"

    def test_reply_with_offer_flips_open_list_to_collecting(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        buyer_card: VendorCard,
        trader: User,
    ):
        """An inbound reply carrying a bid on an OPEN list signals active collection —
        the list flips OPEN -> COLLECTING, mirroring the User-driven submit_offer
        path."""
        excess_list.status = ExcessListStatus.OPEN
        db_session.commit()
        self._make_outreach(db_session, excess_list, buyer_card, trader)

        svc.record_response(
            db_session,
            conversation_id="conv-1",
            has_offer=True,
            offer_lines=[{"mpn_raw": "LM358N", "quantity": 500, "unit_price": "1.25"}],
        )

        db_session.refresh(excess_list)
        assert excess_list.status == ExcessListStatus.COLLECTING

    def test_reply_with_offer_past_close_at_stamps_late(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        buyer_card: VendorCard,
        trader: User,
    ):
        """Finding #10: a reply landing after ``close_at`` lapsed — but before the
        nightly sweep flips the status — is linked as a ``late`` offer, not an
        indistinguishable on-time ``open``, mirroring ``submit_offer``."""
        from datetime import UTC, datetime, timedelta

        excess_list.close_at = datetime.now(UTC) - timedelta(hours=2)
        db_session.commit()
        assert excess_list.status == ExcessListStatus.OPEN  # the nightly sweep hasn't run
        self._make_outreach(db_session, excess_list, buyer_card, trader)

        svc.record_response(
            db_session,
            conversation_id="conv-1",
            has_offer=True,
            offer_lines=[{"mpn_raw": "LM358N", "quantity": 500, "unit_price": "1.25"}],
        )

        offers = db_session.query(ExcessOffer).filter(ExcessOffer.excess_list_id == excess_list.id).all()
        assert len(offers) == 1
        assert offers[0].status == "late"

    def test_reply_with_offer_locks_list_before_status_read(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        buyer_card: VendorCard,
        trader: User,
        monkeypatch,
    ):
        """Finding #9: ``_link_inbound_offer`` takes the M9 list-row lock before reading
        ``excess_list.status`` — spy the hook to prove it's wired."""
        from app.services import excess_service

        excess_list.status = ExcessListStatus.OPEN
        db_session.commit()
        self._make_outreach(db_session, excess_list, buyer_card, trader)

        calls: list[int] = []
        real_lock = excess_service._lock_list_row

        def _spy(db, excess_list_id):
            calls.append(excess_list_id)
            return real_lock(db, excess_list_id)

        monkeypatch.setattr(excess_service, "_lock_list_row", _spy)

        svc.record_response(
            db_session,
            conversation_id="conv-1",
            has_offer=True,
            offer_lines=[{"mpn_raw": "LM358N", "quantity": 500, "unit_price": "1.25"}],
        )

        assert calls == [excess_list.id]

    @pytest.mark.parametrize("bad_qty", [0, -5])
    def test_reply_with_non_positive_quantity_rejected(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        buyer_card: VendorCard,
        trader: User,
        bad_qty: int,
    ):
        """Non-positive inbound offer-line quantity is rejected before any row is built.

        A clear ValueError is raised up front: a 0 is not silently promoted to 1 by the
        old ``or 1`` coercion, and a negative never reaches the ExcessOfferLine
        ``@validates`` 500 mid-write (finding: silent-failure a, service root-cause).
        """
        self._make_outreach(db_session, excess_list, buyer_card, trader)
        with pytest.raises(ValueError):
            svc.record_response(
                db_session,
                conversation_id="conv-1",
                has_offer=True,
                offer_lines=[{"mpn_raw": "LM358N", "quantity": bad_qty, "unit_price": "1.25"}],
            )
        # No partial ExcessOffer was left behind by the rejected link.
        db_session.rollback()
        assert db_session.query(ExcessOffer).filter(ExcessOffer.excess_list_id == excess_list.id).count() == 0

    @pytest.mark.parametrize("bad_qty", [3_000_000_000, 9_999_999_999])
    def test_reply_with_overflow_quantity_rejected(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        buyer_card: VendorCard,
        trader: User,
        bad_qty: int,
    ):
        """An inbound offer-line quantity above the Postgres INT4 ceiling is rejected up
        front — the shared ``_link_inbound_offer`` guard now bounds the value, so an AI-
        extracted whale quantity from the inbox path never reaches the ExcessOfferLine
        INSERT as a NumericValueOutOfRange 500 (SQLite masks the 32-bit bound in
        tests)."""
        self._make_outreach(db_session, excess_list, buyer_card, trader)
        with pytest.raises(ValueError):
            svc.record_response(
                db_session,
                conversation_id="conv-1",
                has_offer=True,
                offer_lines=[{"mpn_raw": "LM358N", "quantity": bad_qty, "unit_price": "1.25"}],
            )
        db_session.rollback()
        assert db_session.query(ExcessOffer).filter(ExcessOffer.excess_list_id == excess_list.id).count() == 0

    def test_reply_declined_advances_to_declined(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, trader: User
    ):
        self._make_outreach(db_session, excess_list, buyer_card, trader)
        updated = svc.record_response(
            db_session,
            conversation_id="conv-1",
            has_offer=False,
            declined=True,
        )
        assert updated[0].status == "declined"

    def test_match_by_message_id(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, trader: User
    ):
        self._make_outreach(db_session, excess_list, buyer_card, trader, conv="cX", msg="mX")
        updated = svc.record_response(db_session, message_id="mX", has_offer=False)
        assert len(updated) == 1
        assert updated[0].status == "responded"

    def test_unmatched_reply_returns_empty(self, db_session: Session):
        assert svc.record_response(db_session, conversation_id="nope", has_offer=False) == []

    def test_terminal_status_not_regressed(
        self, db_session: Session, excess_list: ExcessList, buyer_card: VendorCard, trader: User
    ):
        o = self._make_outreach(db_session, excess_list, buyer_card, trader)
        o.status = "bid"
        db_session.commit()
        updated = svc.record_response(db_session, conversation_id="conv-1", has_offer=False)
        # A late generic reply must not regress a buyer who already bid.
        assert updated[0].status == "bid"

    def test_requires_a_match_key(self, db_session: Session):
        with pytest.raises(ValueError):
            svc.record_response(db_session, has_offer=False)

    def test_commit_false_flushes_without_committing(
        self,
        db_session: Session,
        excess_list: ExcessList,
        line_item: ExcessLineItem,
        buyer_card: VendorCard,
        trader: User,
    ):
        """Commit=False (the inbox-poll savepoint path) must NOT commit, but the linked
        ExcessOffer is flush-visible in the same session so the caller can finish the
        txn."""
        o = self._make_outreach(db_session, excess_list, buyer_card, trader)
        with patch.object(db_session, "commit", MagicMock()) as mock_commit:
            updated = svc.record_response(
                db_session,
                conversation_id="conv-1",
                has_offer=True,
                offer_lines=[{"mpn_raw": "LM358N", "quantity": 500, "unit_price": "1.25"}],
                commit=False,
            )
        mock_commit.assert_not_called()
        assert updated[0].status == "bid"
        # Flushed, so the inbound offer is visible in THIS session before any commit.
        offers = db_session.query(ExcessOffer).filter(ExcessOffer.excess_list_id == excess_list.id).all()
        assert len(offers) == 1
        assert offers[0].id is not None

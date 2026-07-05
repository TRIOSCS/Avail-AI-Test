"""test_resell_outreach_async.py — the async/background split of the Resell email path.

The "Offer to buyers" email campaign used to send N emails AND do N sequential Microsoft
Graph sent-message lookups INLINE in the request, so the modal hung for a multi-buyer
send. It is now two phases:

  - ``enqueue_outreach_email`` (SYNC, request path) writes the tracker rows in the
    transient ``sending`` state and returns at once — it must NOT touch Graph;
  - ``run_outreach_email_send`` (BACKGROUND job) performs the sends + per-buyer
    sent-message lookups off the request path and advances each row to ``sent`` /
    ``no_response``.

These tests prove: (1) the submit returns without awaiting the send loop (rows land in
``sending``, no Graph call); (2) the background job finalizes the rows; (3) a Graph-lookup
failure degrades gracefully (row kept, not lost); (4) the job is idempotent (never
double-sends on a re-run).

send_batch_rfq / _find_sent_message / GraphClient are mocked AT THE SOURCE, so no network
is touched. The background job's session is bound to the test session via
``session_factory`` (its default app SessionLocal points at a different engine).

Called by: pytest
Depends on: app.services.resell_outreach_service, app.routers.resell, tests.conftest
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOutreachStatus
from app.models import Company, ExcessList, ExcessOutreach, User, VendorCard
from app.models.excess import ExcessLineItem
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
        email="async-trader@trioscs.com",
        name="Async Trader",
        role="trader",
        azure_id="async-trader-001",
        m365_connected=True,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def buyer_card(db_session: Session) -> VendorCard:
    vc = VendorCard(
        normalized_name="async buyer",
        display_name="Async Buyer",
        emails=["sales@asyncbuyer.com"],
    )
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


@pytest.fixture()
def buyer_card_two(db_session: Session) -> VendorCard:
    vc = VendorCard(
        normalized_name="second buyer",
        display_name="Second Buyer",
        emails=["ops@secondbuyer.com"],
    )
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


@pytest.fixture()
def posted_list(db_session: Session, seller_company: Company, trader: User) -> ExcessList:
    el = ExcessList(
        company_id=seller_company.id,
        owner_id=trader.id,
        title="Q2 Excess",
        status=ExcessListStatus.COLLECTING,
    )
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def line_item(db_session: Session, posted_list: ExcessList) -> ExcessLineItem:
    li = ExcessLineItem(excess_list_id=posted_list.id, part_number="LM358N", quantity=500)
    db_session.add(li)
    db_session.commit()
    db_session.refresh(li)
    return li


def _sent_result(email: str):
    async def _fake_send(*_args, **_kwargs):
        return [{"vendor_name": "Async Buyer", "vendor_email": email, "status": "sent"}]

    return _fake_send


# ── Phase 1: enqueue returns fast, no Graph touched ──────────────────


class TestEnqueueOutreachEmail:
    def test_writes_sending_rows_and_plan_without_touching_graph(
        self,
        db_session: Session,
        posted_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        """The request-path phase writes ``sending`` rows + a plan and NEVER sends.

        This is the fix's core assertion: the submit returns without awaiting the per-
        buyer send + Graph-lookup loop. Both are mocked; a call would fail the test.
        """
        send_mock = AsyncMock()
        lookup_mock = AsyncMock()
        with (
            patch("app.email_service.send_batch_rfq", send_mock),
            patch("app.email_service._find_sent_message", lookup_mock),
        ):
            rows, plan = svc.enqueue_outreach_email(
                db_session,
                list_id=posted_list.id,
                owner=trader,
                buyers=[{"vendor_card_id": buyer_card.id}],
                scope="whole_list",
                subject="Excess available",
                body="We have surplus stock you may want.",
            )

        # Neither the send nor the Graph lookup ran on the request path.
        send_mock.assert_not_called()
        lookup_mock.assert_not_called()

        assert len(rows) == 1
        row = rows[0]
        assert row.channel == "email"
        assert row.status == ExcessOutreachStatus.SENDING
        assert row.sent_at is None
        assert row.graph_message_id is None
        assert row.graph_conversation_id is None

        # The plan is a serializable per-buyer send group for the background job.
        assert len(plan) == 1
        group = plan[0]
        assert group["card_id"] == buyer_card.id
        assert group["email"] == "sales@asyncbuyer.com"
        assert group["row_ids"] == [row.id]
        assert group["parts"] == ["LM358N"]


# ── Phase 2: the background job finalizes the rows ───────────────────


class TestRunOutreachEmailSend:
    @pytest.mark.asyncio
    async def test_finalizes_sending_rows_to_sent_and_stamps_graph_ids(
        self,
        db_session: Session,
        posted_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        row_id = rows[0].id
        assert rows[0].status == ExcessOutreachStatus.SENDING

        async def _fake_lookup(_gc, _subject, _email):
            return {"id": "msg-async-1", "conversationId": "conv-async-1"}

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_sent_result("sales@asyncbuyer.com")),
            patch("app.email_service._find_sent_message", side_effect=_fake_lookup),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.run_outreach_email_send(
                list_id=posted_list.id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                groups=plan,
                session_factory=lambda: db_session,
            )

        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.SENT
        assert row.sent_at is not None
        assert row.graph_message_id == "msg-async-1"
        assert row.graph_conversation_id == "conv-async-1"

    @pytest.mark.asyncio
    async def test_graph_lookup_failure_degrades_gracefully_row_kept(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """A sent-message lookup failure must not lose the row: it stays ``sent`` with
        NULL graph ids (reply matching degrades but the touch is still tracked)."""
        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        row_id = rows[0].id

        async def _boom(_gc, _subject, _email):
            raise RuntimeError("graph lookup exploded")

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_sent_result("sales@asyncbuyer.com")),
            patch("app.email_service._find_sent_message", side_effect=_boom),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.run_outreach_email_send(
                list_id=posted_list.id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                groups=plan,
                session_factory=lambda: db_session,
            )

        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row is not None  # never dropped
        assert row.status == ExcessOutreachStatus.SENT
        assert row.graph_message_id is None
        assert row.graph_conversation_id is None

    @pytest.mark.asyncio
    async def test_skipped_recipient_flagged_no_response(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        row_id = rows[0].id

        async def _skipped(*_args, **_kwargs):
            return [{"vendor_email": "sales@asyncbuyer.com", "status": "skipped", "error": "do-not-contact"}]

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_skipped),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.run_outreach_email_send(
                list_id=posted_list.id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                groups=plan,
                session_factory=lambda: db_session,
            )

        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.NO_RESPONSE
        assert row.sent_at is None
        assert row.graph_message_id is None

    @pytest.mark.asyncio
    async def test_total_send_failure_flags_no_response_not_stuck_sending(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """If send_batch_rfq raises, the row must not be stranded in ``sending`` — it is
        flagged ``no_response`` so the tracker poll can stop."""
        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        row_id = rows[0].id

        async def _explode(*_args, **_kwargs):
            raise RuntimeError("graph send outage")

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_explode),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.run_outreach_email_send(
                list_id=posted_list.id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                groups=plan,
                session_factory=lambda: db_session,
            )

        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.NO_RESPONSE

    @pytest.mark.asyncio
    async def test_idempotent_rerun_does_not_double_send(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """Re-running the same plan after a finalize must not send again (only
        ``sending`` rows are ever sent)."""
        # Capture scalar ids up front: the background job owns its session and closes it
        # (correct in prod), which detaches the injected test session's ORM instances.
        list_id = posted_list.id
        owner_id = trader.id
        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=list_id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        row_id = rows[0].id

        send_mock = AsyncMock(return_value=[{"vendor_email": "sales@asyncbuyer.com", "status": "sent"}])
        lookup_mock = AsyncMock(return_value={"id": "m", "conversationId": "c"})
        with (
            patch("app.email_service.send_batch_rfq", send_mock),
            patch("app.email_service._find_sent_message", lookup_mock),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.run_outreach_email_send(
                list_id=list_id,
                owner_id=owner_id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                groups=plan,
                session_factory=lambda: db_session,
            )
            # Second run over the SAME plan — the row is no longer ``sending``.
            await svc.run_outreach_email_send(
                list_id=list_id,
                owner_id=owner_id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                groups=plan,
                session_factory=lambda: db_session,
            )

        assert send_mock.await_count == 1  # never re-sent
        db_session.expire_all()
        assert db_session.get(ExcessOutreach, row_id).status == ExcessOutreachStatus.SENT

    @pytest.mark.asyncio
    async def test_multi_buyer_all_finalized(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
        buyer_card_two: VendorCard,
    ):
        # Capture scalar ids up front (the job closes the injected session → detaches).
        list_id = posted_list.id
        owner_id = trader.id
        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=list_id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}, {"vendor_card_id": buyer_card_two.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        assert len(rows) == 2

        async def _both_sent(*_args, **_kwargs):
            return [
                {"vendor_email": "sales@asyncbuyer.com", "status": "sent"},
                {"vendor_email": "ops@secondbuyer.com", "status": "sent"},
            ]

        async def _lookup(_gc, _subject, _email):
            return {"id": f"m-{_email}", "conversationId": f"c-{_email}"}

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_both_sent),
            patch("app.email_service._find_sent_message", side_effect=_lookup),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.run_outreach_email_send(
                list_id=list_id,
                owner_id=owner_id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                groups=plan,
                session_factory=lambda: db_session,
            )

        db_session.expire_all()
        finalized = db_session.query(ExcessOutreach).filter_by(excess_list_id=list_id).all()
        assert len(finalized) == 2
        assert all(r.status == ExcessOutreachStatus.SENT for r in finalized)
        assert all(r.graph_conversation_id for r in finalized)


# ── Router: the submit returns immediately with ``sending`` rows ─────


def _own(user: User):
    """Override require_user to *user* (the list owner).

    Returns a cleanup callable.
    """
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user
    return lambda: app.dependency_overrides.pop(require_user, None)


def test_submit_email_returns_immediately_with_sending_rows(
    client,
    db_session: Session,
    posted_list: ExcessList,
    line_item: ExcessLineItem,
    trader: User,
    buyer_card: VendorCard,
):
    """POST /outreach (email) returns the tracker at once with rows in ``sending`` and
    enqueues the send as a background job — the request itself never calls
    send_batch_rfq.

    The background job is stubbed here so the response reflects the OPTIMISTIC state the
    modal sees; the finalize is covered by TestRunOutreachEmailSend above.
    """
    send_mock = AsyncMock()
    run_stub = MagicMock()
    restore = _own(trader)
    try:
        with (
            patch("app.email_service.send_batch_rfq", send_mock),
            patch("app.services.resell_outreach_service.run_outreach_email_send", run_stub),
        ):
            resp = client.post(
                f"/api/resell/{posted_list.id}/outreach",
                data={
                    "vendor_card_ids": str(buyer_card.id),
                    "scope": "whole_list",
                    "channel": "email",
                    "subject": "Excess offer",
                    "body": "We have these parts available.",
                },
            )
        assert resp.status_code == 200
        # The request path did NOT run the send loop — that is the background job's work.
        send_mock.assert_not_called()
        run_stub.assert_called_once()

        # The tracker re-render optimistically shows the buyer in the ``sending`` state.
        body = resp.text
        assert "Async Buyer" in body
        assert "sending" in body.lower()

        rows = db_session.query(ExcessOutreach).filter_by(excess_list_id=posted_list.id).all()
        assert len(rows) == 1
        assert rows[0].status == ExcessOutreachStatus.SENDING
        assert rows[0].graph_conversation_id is None
    finally:
        restore()

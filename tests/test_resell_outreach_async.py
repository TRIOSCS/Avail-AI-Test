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
from sqlalchemy import event
from sqlalchemy.orm import Session

from app import email_service
from app.constants import ActivityType, ExcessListStatus, ExcessOutreachStatus
from app.models import ActivityLog, Company, ExcessList, ExcessOutreach, User, VendorCard
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


# ── Task 1: FAILED / INTERRUPTED statuses + persisted send_error column ────


class TestOutreachFailedStates:
    def test_failed_and_interrupted_enum_members_exist(self):
        assert ExcessOutreachStatus.FAILED == "failed"
        assert ExcessOutreachStatus.INTERRUPTED == "interrupted"

    def test_model_validates_interrupted_status(self):
        # The status validator auto-accepts the new members (no per-member edit).
        row = ExcessOutreach(excess_list_id=1, submitted_by=1, status="interrupted")
        assert row.status == "interrupted"
        row2 = ExcessOutreach(excess_list_id=1, submitted_by=1, status="failed")
        assert row2.status == "failed"

    def test_send_error_round_trips(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        row = ExcessOutreach(
            excess_list_id=posted_list.id,
            submitted_by=trader.id,
            target_vendor_card_id=buyer_card.id,
            status=ExcessOutreachStatus.FAILED,
            send_error="graph send outage: 503",
        )
        db_session.add(row)
        db_session.commit()
        row_id = row.id
        db_session.expire_all()
        reloaded = db_session.get(ExcessOutreach, row_id)
        assert reloaded.status == ExcessOutreachStatus.FAILED
        assert reloaded.send_error == "graph send outage: 503"


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


# ── #20: offered-lines snapshot is precomputed ONCE per campaign ──────


class TestOutreachSnapshotBatching:
    """#20: the ``parts_included`` offered-lines snapshot is precomputed ONCE per
    campaign (one ``excess_line_items`` scan), not re-queried per (buyer × line)."""

    @staticmethod
    def _seed_multi(db: Session, posted_list: ExcessList) -> tuple[list[ExcessLineItem], list[VendorCard]]:
        lines = []
        for pn, qty in (("LM358N", 100), ("NE555P", 200), ("TL072", 300)):
            li = ExcessLineItem(excess_list_id=posted_list.id, part_number=pn, quantity=qty)
            db.add(li)
            lines.append(li)
        cards = []
        for i in range(3):
            vc = VendorCard(
                normalized_name=f"batch buyer {i}", display_name=f"Batch Buyer {i}", emails=[f"b{i}@ba.com"]
            )
            db.add(vc)
            cards.append(vc)
        db.commit()
        for obj in (*lines, *cards):
            db.refresh(obj)
        return lines, cards

    def test_line_item_snapshot_query_runs_once_per_campaign(
        self, db_session: Session, posted_list: ExcessList, trader: User
    ):
        _lines, cards = self._seed_multi(db_session, posted_list)

        selects: list[str] = []

        def _on_exec(conn, cursor, statement, params, context, executemany):
            if statement.lstrip()[:6].upper() == "SELECT" and "excess_line_items" in statement:
                selects.append(statement)

        bind = db_session.get_bind()
        event.listen(bind, "before_cursor_execute", _on_exec)
        try:
            with (
                patch("app.email_service.send_batch_rfq", AsyncMock()),
                patch("app.email_service._find_sent_message", AsyncMock()),
            ):
                rows, _plan = svc.enqueue_outreach_email(
                    db_session,
                    list_id=posted_list.id,
                    owner=trader,
                    buyers=[{"vendor_card_id": c.id} for c in cards],
                    scope="per_line",
                    subject="Excess available",
                    body="Surplus stock.",
                )
        finally:
            event.remove(bind, "before_cursor_execute", _on_exec)

        # 3 buyers × 3 lines: the old per-(buyer×line) _parts_snapshot issued ~18 line-item
        # SELECTs; the batched snapshot precomputes once (plus _target_line_ids' validation
        # scan) — constant, independent of buyer/line count.
        assert len(selects) <= 3, f"line-item snapshot not batched: {len(selects)} SELECTs\n" + "\n".join(selects)
        assert len(rows) == 9  # 3 buyers × 3 lines

    def test_parts_included_payload_byte_identical_email(
        self,
        db_session: Session,
        posted_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        """The batched snapshot preserves the EXACT parts_included dict keys/values that
        the retry + reply paths read back."""
        with (
            patch("app.email_service.send_batch_rfq", AsyncMock()),
            patch("app.email_service._find_sent_message", AsyncMock()),
        ):
            rows, _ = svc.enqueue_outreach_email(
                db_session,
                list_id=posted_list.id,
                owner=trader,
                buyers=[{"vendor_card_id": buyer_card.id}],
                scope="per_line",
                subject="s",
                body="b",
            )
        assert len(rows) == 1
        assert rows[0].parts_included == [{"part_number": "LM358N", "quantity": 500, "line_item_id": line_item.id}]

    def test_parts_included_payload_manual_whole_list(
        self,
        db_session: Session,
        posted_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        """Manual-log whole-list touch: parts_included is the whole-list snapshot with the
        exact keys (unchanged by the batching)."""
        rows = svc.submit_outreach(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            channel="phone",
        )
        assert len(rows) == 1
        assert rows[0].parts_included == [{"part_number": "LM358N", "quantity": 500, "line_item_id": line_item.id}]


# ── Task 6: campaign idempotency (a double-submit makes no duplicate) ─


class TestCampaignIdempotency:
    def test_second_identical_submit_creates_no_duplicate_row(
        self,
        db_session: Session,
        posted_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        """A second identical submit (double-click / retried request) must not create a
        second live row nor a second send-plan entry — the buyer already has a live
        SENDING/SENT row for the same (list, line)."""
        first_rows, first_plan = svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        assert len(first_rows) == 1
        assert len(first_plan) == 1

        second_rows, second_plan = svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        # Deduped: no new row, no new send.
        assert second_rows == []
        assert second_plan == []
        total = db_session.query(ExcessOutreach).filter_by(excess_list_id=posted_list.id).count()
        assert total == 1

    def test_reoffer_outside_window_is_allowed(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """A prior offer OLDER than the dedup window is a legitimate re-offer, not a
        duplicate — a new row is created."""
        from datetime import UTC, datetime, timedelta

        old = ExcessOutreach(
            excess_list_id=posted_list.id,
            target_vendor_card_id=buyer_card.id,
            submitted_by=trader.id,
            channel="email",
            status=ExcessOutreachStatus.SENT,
            sent_at=datetime.now(UTC) - timedelta(days=3),
            created_at=datetime.now(UTC) - timedelta(days=3),
        )
        db_session.add(old)
        db_session.commit()

        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        assert len(rows) == 1  # re-offer allowed
        assert len(plan) == 1

    def test_distinct_buyer_not_deduped(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
        buyer_card_two: VendorCard,
    ):
        """A live row for buyer A must not suppress a first offer to buyer B."""
        svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card_two.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )
        assert len(rows) == 1
        assert plan[0]["card_id"] == buyer_card_two.id


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
        NULL graph ids AND a degraded-reply-matching note (the touch is still tracked)."""
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
        # Delivered (SENT), never regressed to a failure state — the SEND succeeded; only
        # the reply-matching lookup degraded.
        assert row.status == ExcessOutreachStatus.SENT
        assert row.graph_message_id is None
        assert row.graph_conversation_id is None
        # A degraded flag is stamped so the tracker can say "delivered, reply-matching
        # degraded" (finding: graph-id-missing must not silently look like a clean send).
        assert row.send_error and "degrad" in row.send_error.lower()

    @pytest.mark.asyncio
    async def test_outcome_commit_failure_reapplies_delivered_send(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """Finding #4: if the send-outcome commit ITSELF fails (a serialization error /
        dropped connection), the delivered rows must NOT be left rolled back to
        ``sending`` — that would be swept to ``interrupted`` and re-offered.

        The outcome is snapshotted and RE-APPLIED in a fresh transaction, so the row is
        durably SENT.
        """
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

        async def _found(_gc, _subject, _email):
            return {"id": "m1", "conversationId": "c1"}

        real_commit = db_session.commit
        state = {"failed_once": False}

        def flaky_commit():
            # Fail ONLY the first commit (the send-outcome commit), then behave normally so
            # the re-apply in a fresh transaction can persist the delivered outcome.
            if not state["failed_once"]:
                state["failed_once"] = True
                raise RuntimeError("serialization failure on outcome commit")
            return real_commit()

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_sent_result("sales@asyncbuyer.com")),
            patch("app.email_service._find_sent_message", side_effect=_found),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
            patch.object(db_session, "commit", side_effect=flaky_commit),
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

        assert state["failed_once"]  # the first (outcome) commit really did fail
        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.SENT  # re-applied, never left 'sending'
        assert row.sent_at is not None
        assert row.graph_message_id == "m1"

    @pytest.mark.asyncio
    async def test_skipped_recipient_flagged_failed_with_error(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """A skipped recipient (DNC / no email) is a SEND FAILURE, not buyer silence:

        the row is ``failed`` with the skip reason persisted in ``send_error`` — never
        ``no_response`` (which would libel the buyer as contacted-and-silent).
        """
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
        assert row.status == ExcessOutreachStatus.FAILED
        assert row.send_error == "do-not-contact"
        assert row.sent_at is None
        assert row.graph_message_id is None

    @pytest.mark.asyncio
    async def test_genuine_per_buyer_send_failure_flagged_failed(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """A per-buyer send error (status='failed') → ``failed`` + the error
        persisted."""
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

        async def _failed(*_args, **_kwargs):
            return [{"vendor_email": "sales@asyncbuyer.com", "status": "failed", "error": "smtp 550 mailbox full"}]

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_failed),
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
        assert row.status == ExcessOutreachStatus.FAILED
        assert row.send_error == "smtp 550 mailbox full"
        assert row.sent_at is None

    @pytest.mark.asyncio
    async def test_total_send_failure_flags_failed_not_stuck_sending(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """If send_batch_rfq raises for the whole batch, the row must not be stranded in
        ``sending`` NOR mislabeled ``no_response`` — it is flagged ``failed`` with the
        exception text so the tracker poll stops and the trader sees the real reason."""
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
        assert row.status == ExcessOutreachStatus.FAILED
        assert row.send_error and "graph send outage" in row.send_error

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


# ── Task 3: commit-after-send + guarded bookkeeping + activity gating ─


class TestCommitAfterSendAndActivityGating:
    @pytest.mark.asyncio
    async def test_bookkeeping_exception_does_not_revert_delivered_sent(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """A post-send bookkeeping failure (activity/cadence write) must NOT roll back
        the already-delivered SENT status + graph ids — the email went out, so the
        tracker must reflect it regardless of a downstream write error (regression for
        the blanket except->rollback)."""
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

        async def _lookup(_gc, _subject, _email):
            return {"id": "msg-bk-1", "conversationId": "conv-bk-1"}

        def _explode_bookkeeping(*_args, **_kwargs):
            raise RuntimeError("cadence clock write blew up")

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_sent_result("sales@asyncbuyer.com")),
            patch("app.email_service._find_sent_message", side_effect=_lookup),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
            patch("app.services.resell_outreach_service._log_outreach_activity", side_effect=_explode_bookkeeping),
        ):
            # Must NOT raise — the bookkeeping error is guarded.
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
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.SENT  # delivered SENT survived
        assert row.graph_message_id == "msg-bk-1"
        assert row.graph_conversation_id == "conv-bk-1"

    @pytest.mark.asyncio
    async def test_sent_send_writes_one_emailed_activity(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """A successful send logs exactly one outbound 'Emailed' ActivityLog (happy path
        still bumps cadence)."""
        list_id = posted_list.id
        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=list_id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )

        async def _lookup(_gc, _subject, _email):
            return {"id": "m", "conversationId": "c"}

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_sent_result("sales@asyncbuyer.com")),
            patch("app.email_service._find_sent_message", side_effect=_lookup),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.run_outreach_email_send(
                list_id=list_id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                groups=plan,
                session_factory=lambda: db_session,
            )

        db_session.expire_all()
        acts = db_session.query(ActivityLog).filter(ActivityLog.excess_list_id == list_id).all()
        assert len(acts) == 1
        assert acts[0].activity_type == ActivityType.EMAIL_SENT

    @pytest.mark.asyncio
    async def test_failed_send_writes_no_activity_and_no_cadence_bump(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """A FAILED send must write NO ActivityLog (neither 'Emailed' nor a NOTE) and so
        must NOT advance the cadence clocks — a send that never landed must not look
        like the buyer was contacted (finding #6)."""
        list_id = posted_list.id
        rows, plan = svc.enqueue_outreach_email(
            db_session,
            list_id=list_id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject="Excess available",
            body="surplus",
        )

        async def _skipped(*_args, **_kwargs):
            return [{"vendor_email": "sales@asyncbuyer.com", "status": "skipped", "error": "do-not-contact"}]

        with (
            patch("app.email_service.send_batch_rfq", side_effect=_skipped),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.run_outreach_email_send(
                list_id=list_id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                groups=plan,
                session_factory=lambda: db_session,
            )

        db_session.expire_all()
        acts = db_session.query(ActivityLog).filter(ActivityLog.excess_list_id == list_id).all()
        assert acts == []  # no activity, so no cadence bump


# ── Task 4: retry with the reconcile-first double-send guard ─────────


def _fail_row(db_session: Session, posted_list, trader, buyer_card) -> int:
    """Enqueue one email row then drive it to FAILED; return its id."""
    rows, _plan = svc.enqueue_outreach_email(
        db_session,
        list_id=posted_list.id,
        owner=trader,
        buyers=[{"vendor_card_id": buyer_card.id}],
        scope="whole_list",
        subject="Excess available",
        body="surplus",
    )
    row = rows[0]
    row.status = ExcessOutreachStatus.FAILED
    row.send_error = "graph send outage"
    db_session.commit()
    return row.id


class TestRetryOutreachSend:
    @pytest.mark.asyncio
    async def test_retry_reconciles_already_delivered_and_does_not_resend(
        self,
        db_session: Session,
        posted_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        """Double-send guard: a FAILED row whose email is ALREADY in the Sent folder was
        actually delivered (the failure was downstream) — retry reconciles it to SENT +
        stamps the found ids and NEVER resends."""
        row_id = _fail_row(db_session, posted_list, trader, buyer_card)

        send_mock = AsyncMock()

        async def _found(_gc, _subject, _email):
            return {"id": "already-sent-1", "conversationId": "conv-already-1"}

        with (
            patch("app.email_service.send_batch_rfq", send_mock),
            patch("app.email_service._find_sent_message", side_effect=_found),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.retry_outreach_send(
                outreach_id=row_id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                session_factory=lambda: db_session,
            )

        send_mock.assert_not_called()  # the guard prevented a double-send
        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.SENT
        assert row.graph_message_id == "already-sent-1"
        assert row.graph_conversation_id == "conv-already-1"
        assert row.send_error is None

    @pytest.mark.asyncio
    async def test_retry_resends_when_not_in_sent_folder(
        self,
        db_session: Session,
        posted_list: ExcessList,
        line_item: ExcessLineItem,
        trader: User,
        buyer_card: VendorCard,
    ):
        """When the pre-send reconcile finds nothing, the original never went out: retry
        resets the row to sending and re-sends exactly once, then stamps the new ids."""
        row_id = _fail_row(db_session, posted_list, trader, buyer_card)

        send_mock = AsyncMock(return_value=[{"vendor_email": "sales@asyncbuyer.com", "status": "sent"}])
        calls: list[int] = []

        async def _lookup(_gc, _subject, _email):
            # 1st call = the pre-send reconcile guard (not delivered → None);
            # 2nd call = the post-send stamp inside _finalize_outreach_send.
            calls.append(1)
            return None if len(calls) == 1 else {"id": "resent-1", "conversationId": "conv-resent-1"}

        with (
            patch("app.email_service.send_batch_rfq", send_mock),
            patch("app.email_service._find_sent_message", side_effect=_lookup),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.retry_outreach_send(
                outreach_id=row_id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                session_factory=lambda: db_session,
            )

        assert send_mock.await_count == 1  # resent exactly once
        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.SENT
        assert row.graph_message_id == "resent-1"
        assert row.send_error is None

    @pytest.mark.asyncio
    async def test_retry_customized_subject_matches_delivered_and_never_resends(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """Finding #3: a campaign sent with a CUSTOMIZED subject persists that subject,
        so the double-send guard queries the Sent folder on the REAL subject (not the
        seeded default the router passes) — the already-delivered message matches and
        the offer is never resent."""
        custom_subject = "Q3 clearance — TI parts"
        rows, _plan = svc.enqueue_outreach_email(
            db_session,
            list_id=posted_list.id,
            owner=trader,
            buyers=[{"vendor_card_id": buyer_card.id}],
            scope="whole_list",
            subject=custom_subject,
            body="custom body copy",
        )
        row = rows[0]
        assert row.send_subject == custom_subject  # the exact subject is persisted
        row.status = ExcessOutreachStatus.FAILED
        row.send_error = "graph send outage"
        db_session.commit()
        row_id = row.id

        send_mock = AsyncMock()
        seen_subjects: list[str] = []

        async def _found(_gc, subject, _email):
            seen_subjects.append(subject)
            # Only the REAL (customized) subject matches the delivered message.
            return {"id": "already-1", "conversationId": "conv-1"} if subject == custom_subject else None

        with (
            patch("app.email_service.send_batch_rfq", send_mock),
            patch("app.email_service._find_sent_message", side_effect=_found),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            # The router hands the SEEDED default subject — the guard must ignore it in
            # favour of the persisted one.
            await svc.retry_outreach_send(
                outreach_id=row_id,
                owner_id=trader.id,
                subject="Excess available: Q2 Excess",
                body="default body",
                token="fake-token",
                session_factory=lambda: db_session,
            )

        send_mock.assert_not_called()  # matched the delivered message → no double-send
        assert seen_subjects == [custom_subject]  # guard used the PERSISTED subject
        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.SENT
        assert row.graph_message_id == "already-1"

    @pytest.mark.asyncio
    async def test_retry_lookup_error_leaves_interrupted_and_never_resends(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """Finding #2 (2026-07-22 deep review): a Sent-folder lookup that raises
        ``DeliveryCheckUnavailable`` (every attempt hit a Graph error — 429/5xx/expired
        token) is the UNKNOWN case (the original may have delivered) — the retry must
        NEVER assume not-sent and resend.

        The row is left ``interrupted`` (retryable, no
        false delivery claim) with a reason, and nothing is sent.
        """
        row_id = _fail_row(db_session, posted_list, trader, buyer_card)
        send_mock = AsyncMock()

        async def _boom(_gc, _subject, _email):
            raise email_service.DeliveryCheckUnavailable("graph 429 timeout on every retry attempt")

        with (
            patch("app.email_service.send_batch_rfq", send_mock),
            patch("app.email_service._find_sent_message", side_effect=_boom),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.retry_outreach_send(
                outreach_id=row_id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                session_factory=lambda: db_session,
            )

        send_mock.assert_not_called()  # a lookup error must never trigger a (possible double-) send
        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.INTERRUPTED
        assert row.sent_at is None
        assert row.send_error and "double-send" in row.send_error

    @pytest.mark.asyncio
    async def test_retry_graph_outage_on_every_attempt_never_resends_end_to_end(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
        buyer_card: VendorCard,
    ):
        """Finding #2 (a), end-to-end: the REAL (unmocked) ``email_service._find_sent_
        message`` raises ``DeliveryCheckUnavailable`` when EVERY Graph lookup attempt
        errors (429/5xx/expired token) — the retry must not resend, the row stays
        ``interrupted``, and the persisted message says delivery could not be
        verified."""
        row_id = _fail_row(db_session, posted_list, trader, buyer_card)
        send_mock = AsyncMock()

        failing_gc = AsyncMock()
        failing_gc.get_json.side_effect = RuntimeError("Graph 429 Too Many Requests")

        with (
            patch("app.email_service.send_batch_rfq", send_mock),
            patch("app.utils.graph_client.GraphClient", return_value=failing_gc),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await svc.retry_outreach_send(
                outreach_id=row_id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                session_factory=lambda: db_session,
            )

        send_mock.assert_not_called()  # the real lookup's raise must never trigger a resend
        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.INTERRUPTED
        assert row.sent_at is None
        assert row.send_error and "double-send" in row.send_error

    @pytest.mark.asyncio
    async def test_retry_no_buyer_email_left_failed_without_lookup_or_send(
        self,
        db_session: Session,
        posted_list: ExcessList,
        trader: User,
    ):
        """Finding #9: a FAILED row whose buyer card has no resolvable email is left
        FAILED with a clear reason BEFORE any Sent-folder lookup or resend — it never
        builds a one-buyer plan with email=None."""
        card = VendorCard(normalized_name="no email buyer", display_name="No Email Buyer", emails=[])
        db_session.add(card)
        db_session.commit()
        row = ExcessOutreach(
            excess_list_id=posted_list.id,
            submitted_by=trader.id,
            target_vendor_card_id=card.id,
            channel="email",
            status=ExcessOutreachStatus.FAILED,
            send_error="graph send outage",
        )
        db_session.add(row)
        db_session.commit()
        row_id = row.id

        send_mock = AsyncMock()
        lookup_mock = AsyncMock()
        with (
            patch("app.email_service.send_batch_rfq", send_mock),
            patch("app.email_service._find_sent_message", lookup_mock),
            patch("app.utils.graph_client.GraphClient", return_value=AsyncMock()),
        ):
            await svc.retry_outreach_send(
                outreach_id=row_id,
                owner_id=trader.id,
                subject="Excess available",
                body="surplus",
                token="fake-token",
                session_factory=lambda: db_session,
            )

        send_mock.assert_not_called()
        lookup_mock.assert_not_awaited()  # returns BEFORE the Sent-folder lookup
        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.FAILED
        assert row.send_error == "no buyer email on file to retry"


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


def test_retry_route_flips_failed_to_sending_and_enqueues(
    client,
    db_session: Session,
    posted_list: ExcessList,
    trader: User,
    buyer_card: VendorCard,
):
    """POST .../retry on a FAILED row flips it to ``sending`` at once and enqueues the
    reconcile-first background retry (the request never resends inline)."""
    row_id = _fail_row(db_session, posted_list, trader, buyer_card)
    # #11/#12: the customer-named title must NEVER reach the retry resend subject. The
    # service resends with ``row.send_subject or subject`` — so for a legacy / cleared-
    # subject row (send_subject NULL) this fallback ships EXTERNALLY to the buyer. Give the
    # list a customer name and prove the enqueued fallback is neutralized.
    posted_list.title = "Acme Corp — surplus FPGAs"
    db_session.commit()
    retry_stub = MagicMock()
    restore = _own(trader)
    try:
        with patch("app.services.resell_outreach_service.retry_outreach_send", retry_stub):
            resp = client.post(f"/api/resell/{posted_list.id}/outreach/{row_id}/retry")
        assert resp.status_code == 200
        retry_stub.assert_called_once()
        # Finding #10: the double-send guard's key input is WHICH row gets reconciled — assert
        # the background task is enqueued with the right outreach_id/owner + a subject/body/token,
        # not merely that *something* was scheduled.
        kwargs = retry_stub.call_args.kwargs
        assert kwargs["outreach_id"] == row_id
        assert kwargs["owner_id"] == trader.id
        # The fallback subject is the neutral part-count default, never the customer title.
        assert posted_list.title not in kwargs["subject"], "customer-named title leaked into the retry resend subject"
        assert "Excess available" in kwargs["subject"]  # neutral, part-count fallback
        assert kwargs["body"]
        assert kwargs["token"]
        db_session.expire_all()
        row = db_session.get(ExcessOutreach, row_id)
        assert row.status == ExcessOutreachStatus.SENDING
        assert row.send_error is None
    finally:
        restore()


def test_retry_route_refreshes_created_at_so_sweeper_cannot_flip(
    client,
    db_session: Session,
    posted_list: ExcessList,
    trader: User,
    buyer_card: VendorCard,
):
    """Finding #6: the optimistic retry flip refreshes ``created_at`` so the row is not
    'born stale'.

    The nightly stale-sending sweeper selects on ``created_at < now - 30min``;
    a retried row still carrying its original (hours-old) enqueue time would be flipped to
    ``interrupted`` mid-resend. After the refresh the sweeper leaves the in-flight retry
    alone.
    """
    from datetime import UTC, datetime, timedelta

    row_id = _fail_row(db_session, posted_list, trader, buyer_card)
    # Age the row well past the staleness threshold (its original enqueue time).
    aged = db_session.get(ExcessOutreach, row_id)
    aged.created_at = datetime.now(UTC) - timedelta(hours=5)
    db_session.commit()

    retry_stub = MagicMock()  # keep the background resend out of this router-level test
    restore = _own(trader)
    try:
        with patch("app.services.resell_outreach_service.retry_outreach_send", retry_stub):
            resp = client.post(f"/api/resell/{posted_list.id}/outreach/{row_id}/retry")
        assert resp.status_code == 200
        db_session.expire_all()
        assert db_session.get(ExcessOutreach, row_id).status == ExcessOutreachStatus.SENDING

        # The sweeper must NOT flip the freshly-retried SENDING row (created_at was refreshed).
        flipped = svc.sweep_stale_sending_outreach(db_session, now=datetime.now(UTC))
        assert flipped == 0
        db_session.expire_all()
        assert db_session.get(ExcessOutreach, row_id).status == ExcessOutreachStatus.SENDING
    finally:
        restore()


def test_retry_route_rejects_non_retryable_row(
    client,
    db_session: Session,
    posted_list: ExcessList,
    trader: User,
    buyer_card: VendorCard,
):
    """A row that is not failed/interrupted (e.g. already SENT) cannot be retried —
    409."""
    rows, _plan = svc.enqueue_outreach_email(
        db_session,
        list_id=posted_list.id,
        owner=trader,
        buyers=[{"vendor_card_id": buyer_card.id}],
        scope="whole_list",
        subject="Excess available",
        body="surplus",
    )
    row = rows[0]
    row.status = ExcessOutreachStatus.SENT
    db_session.commit()
    row_id = row.id
    restore = _own(trader)
    try:
        resp = client.post(f"/api/resell/{posted_list.id}/outreach/{row_id}/retry")
        assert resp.status_code == 409
    finally:
        restore()


# ── Task 5: rendered tracker HTML + CSV export truthfulness ──────────


def test_tracker_html_non_sent_row_shows_dash_badge_retry_and_error(
    client,
    db_session: Session,
    posted_list: ExcessList,
    trader: User,
    buyer_card: VendorCard,
    buyer_card_two: VendorCard,
):
    """Finding #8: the RENDERED tracker HTML for a FAILED / INTERRUPTED row shows a dash
    for "When" (never its created_at as an 'offered at' time), the correct failed/amber
    badge colour, a Retry button, and surfaces send_error — while a genuinely-SENT row
    renders its real send time and NO Retry affordance."""
    from datetime import UTC, datetime, timedelta

    from app.utils.timezones import format_localtime

    failed_created = datetime(2026, 3, 3, 9, 7, tzinfo=UTC)
    sent_when_dt = datetime.now(UTC) - timedelta(minutes=1)
    third = VendorCard(normalized_name="third buyer", display_name="Third Buyer", emails=["z@third.com"])
    db_session.add(third)
    db_session.commit()

    failed = ExcessOutreach(
        excess_list_id=posted_list.id,
        submitted_by=trader.id,
        target_vendor_card_id=buyer_card.id,
        channel="email",
        status=ExcessOutreachStatus.FAILED,
        send_error="graph send outage: 503",
        created_at=failed_created,
    )
    interrupted = ExcessOutreach(
        excess_list_id=posted_list.id,
        submitted_by=trader.id,
        target_vendor_card_id=buyer_card_two.id,
        channel="email",
        status=ExcessOutreachStatus.INTERRUPTED,
        send_error="send interrupted — stuck in 'sending'",
        created_at=failed_created,
    )
    sent = ExcessOutreach(
        excess_list_id=posted_list.id,
        submitted_by=trader.id,
        target_vendor_card_id=third.id,
        channel="email",
        status=ExcessOutreachStatus.SENT,
        sent_at=sent_when_dt,
        created_at=sent_when_dt,
    )
    db_session.add_all([failed, interrupted, sent])
    db_session.commit()

    restore = _own(trader)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach")
    finally:
        restore()
    assert resp.status_code == 200
    html = resp.text

    # Retry button appears ONLY on the two non-sent email rows, never on the sent row.
    assert html.count("/retry") == 2
    # Both non-sent reasons are surfaced (no longer invisible).
    assert "graph send outage: 503" in html
    assert "send interrupted" in html
    # Failed (rose) + interrupted (amber) badge colours are rendered.
    assert "text-rose-600" in html
    assert "text-amber-700" in html
    # The SENT row's real send time renders; the non-sent rows' created_at does NOT (When = —).
    assert format_localtime(sent_when_dt, "%b %d, %H:%M") in html
    assert format_localtime(failed_created, "%b %d, %H:%M") not in html


def test_csv_export_blanks_sent_at_for_non_sent_and_exports_note(
    client,
    db_session: Session,
    posted_list: ExcessList,
    trader: User,
    buyer_card: VendorCard,
    buyer_card_two: VendorCard,
):
    """Finding #5 + #2 (export): the CSV 'Sent At' column is BLANK for a non-sent row
    (never its created_at, which the tracker tab already drops), and the persisted
    send_error is exported in a 'Note' column instead of being silently omitted."""
    from datetime import UTC, datetime

    failed = ExcessOutreach(
        excess_list_id=posted_list.id,
        submitted_by=trader.id,
        target_vendor_card_id=buyer_card.id,
        channel="email",
        status=ExcessOutreachStatus.FAILED,
        send_error="graph send outage: 503",
        # A distinctive naive-formatted stamp (_fmt_dt does not tz-convert) that must NOT
        # appear as a Sent At once the created_at fallback is dropped for non-sent rows.
        created_at=datetime(2026, 1, 2, 3, 4, tzinfo=UTC),
    )
    sent = ExcessOutreach(
        excess_list_id=posted_list.id,
        submitted_by=trader.id,
        target_vendor_card_id=buyer_card_two.id,
        channel="email",
        status=ExcessOutreachStatus.SENT,
        sent_at=datetime(2026, 5, 6, 7, 8, tzinfo=UTC),
        created_at=datetime(2026, 5, 6, 7, 8, tzinfo=UTC),
    )
    db_session.add_all([failed, sent])
    db_session.commit()

    restore = _own(trader)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach/export")
    finally:
        restore()
    assert resp.status_code == 200
    text = resp.text
    assert "Note" in text  # the new Note column header
    assert "graph send outage: 503" in text  # failed reason exported, not hidden
    assert "2026-05-06 07:08" in text  # the SENT row's real send time is exported
    assert "2026-01-02 03:04" not in text  # the FAILED row's created_at is NOT a Sent At

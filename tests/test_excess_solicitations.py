"""tests/test_excess_solicitations.py — Tests for excess bid solicitation send.

Covers bundled send (single email, multiple items), split send (one email per item),
failure handling, and validation.

Called by: pytest
Depends on: app/services/excess_service.py, app/models/excess.py, tests/conftest.py
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList


@pytest.fixture()
def excess_list_with_items(db_session: Session, test_user: User, test_company: Company):
    """Create an ExcessList with 3 ExcessLineItems for solicitation tests."""
    el = ExcessList(
        company_id=test_company.id,
        owner_id=test_user.id,
        title="Test Excess List",
        status="active",
    )
    db_session.add(el)
    db_session.flush()

    items = []
    for i, (pn, mfr, qty) in enumerate(
        [
            ("LM358N", "Texas Instruments", 1000),
            ("SN74HC595N", "Texas Instruments", 500),
            ("NE555P", "STMicro", 2000),
        ]
    ):
        item = ExcessLineItem(
            excess_list_id=el.id,
            part_number=pn,
            manufacturer=mfr,
            quantity=qty,
            condition="New",
            date_code="2025+",
            asking_price=0.50 + i * 0.25,
        )
        db_session.add(item)
        items.append(item)

    db_session.commit()
    db_session.refresh(el)
    for item in items:
        db_session.refresh(item)
    return el, items


def _mock_graph_client():
    """Build a mock GraphClient with post_json and get_json."""
    gc = AsyncMock()
    # sendMail returns None (202 No Content)
    gc.post_json = AsyncMock(return_value=None)
    # Sent items lookup returns a matching message
    gc.get_json = AsyncMock(
        return_value={"value": [{"id": "graph-msg-abc123", "conversationId": "conv-xyz", "subject": "placeholder"}]}
    )
    return gc


class TestSendBundled:
    """Bundled=True creates 3 BidSolicitation records but sends only 1 email."""

    def test_bundled_creates_records_and_sends_once(
        self, client, db_session, test_user, test_company, excess_list_with_items
    ):
        el, items = excess_list_with_items
        item_ids = [it.id for it in items]

        mock_gc = _mock_graph_client()

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            with patch(
                "app.services.excess_service._find_sent_message",
                new_callable=AsyncMock,
            ) as mock_find:
                mock_find.return_value = {
                    "id": "graph-msg-bundled-001",
                    "conversationId": "conv-1",
                }
                resp = client.post(
                    f"/api/excess-lists/{el.id}/solicitations",
                    json={
                        "line_item_ids": item_ids,
                        "recipient_email": "buyer@example.com",
                        "recipient_name": "Joe Buyer",
                        "contact_id": 99,
                        "bundled": True,
                    },
                )

        assert resp.status_code == 201
        data = resp.json()
        assert data["total"] == 3

        # Only 1 sendMail call for bundled mode
        assert mock_gc.post_json.call_count == 1

        # All 3 solicitations share the same graph_message_id
        msg_ids = {s["graph_message_id"] for s in data["items"]}
        assert len(msg_ids) == 1
        assert "graph-msg-bundled-001" in msg_ids

        # Subject contains EXCESS-BID tag
        subjects = {s["subject"] for s in data["items"]}
        assert len(subjects) == 1
        subject = subjects.pop()
        assert "[EXCESS-BID-" in subject

    def test_bundled_subject_uses_first_solicitation_id(
        self, client, db_session, test_user, test_company, excess_list_with_items
    ):
        el, items = excess_list_with_items
        item_ids = [it.id for it in items]

        mock_gc = _mock_graph_client()
        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            with patch(
                "app.services.excess_service._find_sent_message",
                new_callable=AsyncMock,
            ) as mock_find:
                mock_find.return_value = {"id": "msg-1"}
                resp = client.post(
                    f"/api/excess-lists/{el.id}/solicitations",
                    json={
                        "line_item_ids": item_ids,
                        "recipient_email": "buyer@example.com",
                        "contact_id": 99,
                        "bundled": True,
                    },
                )

        assert resp.status_code == 201
        data = resp.json()
        first_id = min(s["id"] for s in data["items"])
        subject = data["items"][0]["subject"]
        assert f"[EXCESS-BID-{first_id}]" in subject


class TestSendSplit:
    """Bundled=False sends 3 separate emails (3 sendMail calls)."""

    def test_split_sends_separate_emails(self, client, db_session, test_user, test_company, excess_list_with_items):
        el, items = excess_list_with_items
        item_ids = [it.id for it in items]

        mock_gc = _mock_graph_client()
        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            with patch(
                "app.services.excess_service._find_sent_message",
                new_callable=AsyncMock,
            ) as mock_find:
                mock_find.return_value = {"id": "msg-split"}
                resp = client.post(
                    f"/api/excess-lists/{el.id}/solicitations",
                    json={
                        "line_item_ids": item_ids,
                        "recipient_email": "buyer@example.com",
                        "recipient_name": "Jane Buyer",
                        "contact_id": 99,
                        "bundled": False,
                    },
                )

        assert resp.status_code == 201
        data = resp.json()
        assert data["total"] == 3

        # 3 separate sendMail calls
        assert mock_gc.post_json.call_count == 3

        # Each solicitation has its own EXCESS-BID tag
        subjects = [s["subject"] for s in data["items"]]
        bid_tags = [subj.split("[EXCESS-BID-")[1].split("]")[0] for subj in subjects]
        assert len(set(bid_tags)) == 3  # all different IDs


class TestSendFailure:
    """Graph API exception sets solicitation status to failed."""

    def test_graph_failure_marks_status_failed(
        self, client, db_session, test_user, test_company, excess_list_with_items
    ):
        el, items = excess_list_with_items
        item_ids = [items[0].id]

        mock_gc = _mock_graph_client()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Graph API timeout"))

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            resp = client.post(
                f"/api/excess-lists/{el.id}/solicitations",
                json={
                    "line_item_ids": item_ids,
                    "recipient_email": "buyer@example.com",
                    "contact_id": 99,
                    "bundled": True,
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["status"] == "failed"

    def test_split_failure_marks_each_failed(self, client, db_session, test_user, test_company, excess_list_with_items):
        el, items = excess_list_with_items
        item_ids = [items[0].id, items[1].id]

        mock_gc = _mock_graph_client()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Network error"))

        with patch("app.utils.graph_client.GraphClient", return_value=mock_gc):
            resp = client.post(
                f"/api/excess-lists/{el.id}/solicitations",
                json={
                    "line_item_ids": item_ids,
                    "recipient_email": "buyer@example.com",
                    "contact_id": 99,
                    "bundled": False,
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert all(s["status"] == "failed" for s in data["items"])


class TestSendValidation:
    """Validation: empty line_item_ids returns 422."""

    def test_empty_line_item_ids_returns_422(self, client, db_session, test_user, test_company, excess_list_with_items):
        el, _ = excess_list_with_items
        resp = client.post(
            f"/api/excess-lists/{el.id}/solicitations",
            json={
                "line_item_ids": [],
                "recipient_email": "buyer@example.com",
                "contact_id": 99,
            },
        )
        assert resp.status_code == 422


class TestPolishEmail:
    """AI email polish endpoint."""

    @patch("app.routers.excess.claude_text", new_callable=AsyncMock)
    def test_polish_returns_cleaned_text(self, mock_claude, client):
        mock_claude.return_value = "Polished version of the email."
        resp = client.post("/api/excess-lists/polish-email", json={"text": "hey we got parts u want sum?"})
        assert resp.status_code == 200
        assert resp.json()["text"] == "Polished version of the email."

    def test_polish_empty_text_returns_422(self, client):
        resp = client.post("/api/excess-lists/polish-email", json={"text": ""})
        assert resp.status_code == 422


class TestRoundTrip:
    """Full round-trip: send solicitation via API, then parse an email reply."""

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    @patch("app.utils.graph_client.GraphClient")
    def test_send_then_parse_creates_bid(
        self,
        mock_graph_cls,
        mock_claude,
        client,
        db_session,
        test_user,
        test_company,
        excess_list_with_items,
    ):
        import asyncio

        from app.models import ActivityLog
        from app.models.excess import Bid, BidSolicitation

        el, items = excess_list_with_items
        item = items[0]  # LM358N

        # Configure GraphClient mock
        mock_gc = AsyncMock()
        mock_gc.post_json = AsyncMock(return_value=None)
        mock_graph_cls.return_value = mock_gc

        # Step 1: Send the solicitation via the API
        with patch(
            "app.services.excess_service._find_sent_message",
            new_callable=AsyncMock,
            return_value={"id": "graph-msg-rt-001", "conversationId": "conv-rt-1"},
        ):
            resp = client.post(
                f"/api/excess-lists/{el.id}/solicitations",
                json={
                    "line_item_ids": [item.id],
                    "recipient_email": "buyer@example.com",
                    "recipient_name": "Round Trip Buyer",
                    "contact_id": 42,
                    "bundled": True,
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["total"] == 1
        sol_id = data["items"][0]["id"]

        # Step 2: Mock Claude to return a valid bid
        mock_claude.return_value = {
            "unit_price": 0.40,
            "quantity_wanted": 800,
            "lead_time_days": 5,
            "notes": "In stock, ready to ship",
        }

        # Step 3: Call _handle_excess_bid_reply directly with a mock message
        msg = {"body": {"content": "We can offer 800 pcs at $0.40 each, 5 day lead time."}}

        from app.email_service import _handle_excess_bid_reply

        asyncio.run(_handle_excess_bid_reply(msg, sol_id, db_session))
        db_session.commit()  # Persist ActivityLog added after parse_bid_response commit

        # Step 4: Verify bid created with source="email_parsed" and unit_price=0.40
        sol = db_session.get(BidSolicitation, sol_id)
        db_session.refresh(sol)
        assert sol.parsed_bid_id is not None

        bid = db_session.get(Bid, sol.parsed_bid_id)
        assert bid is not None
        assert bid.source == "email_parsed"
        assert float(bid.unit_price) == 0.40

        # Step 5: Verify solicitation status="responded" and parsed_bid_id set
        assert sol.status == "responded"
        assert sol.parsed_bid_id == bid.id

        # Step 6: Verify ActivityLog with activity_type="bid_received" contains part number
        logs = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "bid_received").all()
        assert len(logs) >= 1
        assert any(item.part_number in log.subject for log in logs)


class TestInboxParse:
    """Inbox parsing of excess bid solicitation replies."""

    def _make_solicitation(self, db_session, excess_list_with_items, status="sent"):
        """Create a BidSolicitation record linked to the first line item."""
        from app.models.excess import BidSolicitation

        _, items = excess_list_with_items
        item = items[0]
        # Get the owner from the excess list
        el = db_session.get(ExcessList, item.excess_list_id)
        sol = BidSolicitation(
            excess_line_item_id=item.id,
            contact_id=99,
            sent_by=el.owner_id,
            recipient_email="buyer@example.com",
            recipient_name="Joe Buyer",
            subject="[EXCESS-BID-0] RE: Excess parts",
            status=status,
            sent_at=datetime.now(timezone.utc),
        )
        db_session.add(sol)
        db_session.commit()
        db_session.refresh(sol)
        return sol

    def test_excess_bid_tag_creates_pending_bid(self, db_session, excess_list_with_items):
        """Mock claude_structured to return bid data.

        Assert Bid created with correct fields.
        """
        import asyncio

        from app.models.excess import Bid

        sol = self._make_solicitation(db_session, excess_list_with_items)
        msg = {
            "body": {"content": "We can do 5000 pcs at $0.38 each, ship next week."},
        }
        mock_result = {
            "unit_price": 0.38,
            "quantity_wanted": 5000,
            "lead_time_days": 7,
            "notes": "Can ship next week",
        }

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result):
            from app.email_service import _handle_excess_bid_reply

            asyncio.run(_handle_excess_bid_reply(msg, sol.id, db_session))

        db_session.refresh(sol)
        assert sol.status == "responded"
        assert sol.parsed_bid_id is not None

        bid = db_session.get(Bid, sol.parsed_bid_id)
        assert bid is not None
        assert bid.status == "pending"
        assert bid.source == "email_parsed"
        assert float(bid.unit_price) == 0.38
        assert bid.quantity_wanted == 5000

    def test_declined_response_no_bid_created(self, db_session, excess_list_with_items):
        """When claude returns declined=True, no Bid is created but status updated."""
        import asyncio

        from app.models.excess import Bid

        sol = self._make_solicitation(db_session, excess_list_with_items)
        msg = {"body": {"content": "Sorry, not interested at this time."}}
        mock_result = {"declined": True}

        with patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock, return_value=mock_result):
            from app.email_service import _handle_excess_bid_reply

            asyncio.run(_handle_excess_bid_reply(msg, sol.id, db_session))

        db_session.refresh(sol)
        assert sol.status == "responded"

        # No bid should have been created
        bids = db_session.query(Bid).filter(Bid.excess_line_item_id == sol.excess_line_item_id).all()
        assert len(bids) == 0

    def test_already_responded_skipped(self, db_session, excess_list_with_items):
        """Solicitation already responded should be skipped without error."""
        import asyncio

        sol = self._make_solicitation(db_session, excess_list_with_items, status="responded")
        msg = {"body": {"content": "Follow up email"}}

        from app.email_service import _handle_excess_bid_reply

        # Should return without error, no mocking needed since it returns early
        asyncio.run(_handle_excess_bid_reply(msg, sol.id, db_session))

        db_session.refresh(sol)
        assert sol.status == "responded"

    def test_solicitation_not_found_skipped(self, db_session, excess_list_with_items):
        """Non-existent solicitation ID should not raise."""
        import asyncio

        msg = {"body": {"content": "Some email body"}}

        from app.email_service import _handle_excess_bid_reply

        # Should not raise
        asyncio.run(_handle_excess_bid_reply(msg, 99999, db_session))

    def test_lookback_window_skips_old_solicitations(self, db_session, excess_list_with_items):
        """Solicitation sent >lookback_days ago should be skipped."""
        import asyncio

        sol = self._make_solicitation(db_session, excess_list_with_items)
        # Set sent_at to 30 days ago (beyond default 14-day lookback)
        sol.sent_at = datetime.now(timezone.utc) - timedelta(days=30)
        db_session.commit()

        msg = {"body": {"content": "Late reply"}}

        from app.email_service import _handle_excess_bid_reply

        asyncio.run(_handle_excess_bid_reply(msg, sol.id, db_session))

        db_session.refresh(sol)
        assert sol.status == "sent"  # unchanged

    def test_parse_failure_leaves_solicitation_sent(self, db_session, excess_list_with_items):
        """When claude_structured raises, solicitation stays in sent status."""
        import asyncio

        sol = self._make_solicitation(db_session, excess_list_with_items)
        msg = {"body": {"content": "We are interested."}}

        with patch(
            "app.utils.claude_client.claude_structured",
            new_callable=AsyncMock,
            side_effect=Exception("API timeout"),
        ):
            from app.email_service import _handle_excess_bid_reply

            asyncio.run(_handle_excess_bid_reply(msg, sol.id, db_session))

        db_session.refresh(sol)
        assert sol.status == "sent"  # unchanged

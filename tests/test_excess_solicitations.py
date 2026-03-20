"""tests/test_excess_solicitations.py — Tests for excess bid solicitation send.

Covers bundled send (single email, multiple items), split send (one email per item),
failure handling, and validation.

Called by: pytest
Depends on: app/services/excess_service.py, app/models/excess.py, tests/conftest.py
"""

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

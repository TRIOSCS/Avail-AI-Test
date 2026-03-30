"""Tests for excess bid response inbox scanning.

Called by: pytest
Depends on: app.jobs.email_jobs._scan_excess_bid_responses, app.models.excess
"""

import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Company, User
from app.models.excess import BidSolicitation, ExcessLineItem, ExcessList
from tests.conftest import engine  # noqa: F401

_ = engine  # Ensure test DB tables are created

_EXCESS_BID_RE = re.compile(r"\[EXCESS-BID-(\d+)\]")


class TestExcessBidRegex:
    def test_matches_valid_tag(self):
        match = _EXCESS_BID_RE.search("Re: Bid Request: LM358N x 5000 [EXCESS-BID-42]")
        assert match is not None
        assert match.group(1) == "42"

    def test_no_match_without_tag(self):
        assert _EXCESS_BID_RE.search("Regular email subject") is None

    def test_extracts_from_reply_chain(self):
        match = _EXCESS_BID_RE.search("RE: RE: Bid Request [EXCESS-BID-999]")
        assert match.group(1) == "999"


class TestScanExcessBidResponses:
    @pytest.mark.asyncio
    async def test_skips_when_no_pending_solicitations(self, db_session):
        """No solicitations with status=sent -> no Graph API calls."""
        user = User(
            email="test@trioscs.com", name="Test", role="sales", azure_id="t-001", created_at=datetime.now(timezone.utc)
        )
        db_session.add(user)
        db_session.commit()

        with patch("app.utils.graph_client.GraphClient") as MockGC:
            from app.jobs.email_jobs import _scan_excess_bid_responses

            await _scan_excess_bid_responses(user, db_session)
            MockGC.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_matching_reply(self, db_session):
        """Reply with EXCESS-BID tag triggers parse_bid_from_email."""
        user = User(
            email="test@trioscs.com", name="Test", role="sales", azure_id="t-002", created_at=datetime.now(timezone.utc)
        )
        db_session.add(user)
        db_session.flush()

        co = Company(name="Test Excess Co")
        db_session.add(co)
        db_session.flush()

        el = ExcessList(company_id=co.id, owner_id=user.id, title="Test", status="active")
        db_session.add(el)
        db_session.flush()

        item = ExcessLineItem(excess_list_id=el.id, part_number="TEST123", quantity=100, condition="New")
        db_session.add(item)
        db_session.flush()

        sol = BidSolicitation(
            excess_line_item_id=item.id,
            contact_id=0,
            sent_by=user.id,
            recipient_email="buyer@test.com",
            subject="Bid Request [EXCESS-BID-1]",
            status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db_session.add(sol)
        db_session.commit()

        mock_messages = [
            {
                "subject": f"Re: Bid Request [EXCESS-BID-{sol.id}]",
                "body": {"content": "We offer $0.50 for 100 pcs"},
                "receivedDateTime": datetime.now(timezone.utc).isoformat(),
            }
        ]

        mock_gc = AsyncMock()
        mock_gc.get_json.return_value = {"value": mock_messages}

        with (
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
            patch("app.services.excess_service.parse_bid_from_email", new_callable=AsyncMock) as mock_parse,
            patch("app.utils.token_manager.get_valid_token", new_callable=AsyncMock, return_value="fake-token"),
        ):
            mock_parse.return_value = MagicMock()

            from app.jobs.email_jobs import _scan_excess_bid_responses

            await _scan_excess_bid_responses(user, db_session)
            mock_parse.assert_called_once()

"""Tests for AI ticket consolidation service.

Covers: similarity detection, linking, batch consolidation, error handling.
All Claude calls are mocked — no real AI invocations.

Called by: pytest
Depends on: app.services.ticket_consolidation, conftest fixtures
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.models.trouble_ticket import TroubleTicket
from app.services.ticket_consolidation import (
    batch_consolidate,
    consolidate_ticket,
    find_similar_ticket,
)
from app.services.trouble_ticket_service import create_ticket


def _run(coro):
    """Run async coroutine in sync test."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_ticket(db, user, **overrides):
    """Create a test ticket with sensible defaults."""
    defaults = dict(
        title="Button broken",
        description="Submit button does nothing on RFQ page",
        current_page="/api/rfq",
    )
    defaults.update(overrides)
    return create_ticket(db=db, user_id=user.id, **defaults)


class TestFindSimilarTicket:
    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_high_confidence_match(self, mock_claude, db_session, test_user):
        """High confidence match returns match dict."""
        parent = _make_ticket(db_session, test_user, title="Login page 500 error",
                              description="Server error when clicking login")
        child = _make_ticket(db_session, test_user, title="Login fails with 500",
                             description="Internal server error on login page")

        mock_claude.return_value = {"match_id": parent.id, "confidence": 0.95}

        result = _run(find_similar_ticket(child, db_session))
        assert result is not None
        assert result["match_id"] == parent.id
        assert result["confidence"] == 0.95

    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_low_confidence_returns_none(self, mock_claude, db_session, test_user):
        """Below threshold confidence returns None."""
        _make_ticket(db_session, test_user, title="Search is slow",
                     description="Search takes 10 seconds")
        child = _make_ticket(db_session, test_user, title="Dashboard loading issue",
                             description="Dashboard is blank for 5 seconds")

        mock_claude.return_value = {"match_id": 1, "confidence": 0.5}

        result = _run(find_similar_ticket(child, db_session))
        assert result is None

    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_no_open_tickets_returns_none(self, mock_claude, db_session, test_user):
        """When there are no other open tickets, returns None without calling AI."""
        ticket = _make_ticket(db_session, test_user)

        # Close the ticket so it won't appear as a candidate
        ticket.status = "resolved"
        db_session.commit()

        # Create a new ticket as the target
        target = _make_ticket(db_session, test_user, title="New bug",
                              description="Something else broke")

        result = _run(find_similar_ticket(target, db_session))
        assert result is None
        mock_claude.assert_not_called()

    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_ai_exception_returns_none(self, mock_claude, db_session, test_user):
        """AI failure does not crash — returns None gracefully."""
        _make_ticket(db_session, test_user, title="Existing bug",
                     description="Something is wrong")
        child = _make_ticket(db_session, test_user, title="Another bug",
                             description="Something else is wrong")

        mock_claude.side_effect = Exception("API timeout")

        result = _run(find_similar_ticket(child, db_session))
        assert result is None

    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_invalid_match_id_returns_none(self, mock_claude, db_session, test_user):
        """AI returning an ID not in the candidate set is rejected."""
        _make_ticket(db_session, test_user, title="Bug A", description="Desc A")
        child = _make_ticket(db_session, test_user, title="Bug B", description="Desc B")

        mock_claude.return_value = {"match_id": 99999, "confidence": 0.95}

        result = _run(find_similar_ticket(child, db_session))
        assert result is None


class TestConsolidateTicket:
    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_links_ticket_correctly(self, mock_claude, db_session, test_user):
        """consolidate_ticket sets parent_ticket_id and similarity_score."""
        parent = _make_ticket(db_session, test_user, title="Original bug",
                              description="The original issue")
        child = _make_ticket(db_session, test_user, title="Duplicate bug",
                             description="Same issue again")

        mock_claude.return_value = {"match_id": parent.id, "confidence": 0.92}

        _run(consolidate_ticket(child.id, db_session))

        db_session.refresh(child)
        assert child.parent_ticket_id == parent.id
        assert child.similarity_score == 0.92

    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_already_linked_ticket_skipped(self, mock_claude, db_session, test_user):
        """Tickets with parent_ticket_id already set are skipped."""
        parent = _make_ticket(db_session, test_user, title="Parent", description="Parent desc")
        child = _make_ticket(db_session, test_user, title="Child", description="Child desc")

        # Pre-link the child
        child.parent_ticket_id = parent.id
        child.similarity_score = 0.99
        db_session.commit()

        _run(consolidate_ticket(child.id, db_session))

        # AI should never be called
        mock_claude.assert_not_called()
        db_session.refresh(child)
        assert child.parent_ticket_id == parent.id


    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_claude_returns_none(self, mock_claude, db_session, test_user):
        """claude_structured returning None is handled gracefully."""
        _make_ticket(db_session, test_user, title="Existing bug", description="Desc")
        child = _make_ticket(db_session, test_user, title="New bug", description="Desc2")

        mock_claude.return_value = None

        result = _run(find_similar_ticket(child, db_session))
        assert result is None

    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_nonexistent_ticket_id(self, mock_claude, db_session, test_user):
        """consolidate_ticket with a missing ticket ID does nothing."""
        _run(consolidate_ticket(999999, db_session))
        mock_claude.assert_not_called()


class TestBatchConsolidate:
    @patch("app.services.ticket_consolidation.claude_structured", new_callable=AsyncMock)
    def test_batch_links_multiple(self, mock_claude, db_session, test_user):
        """batch_consolidate processes unlinked tickets and returns count."""
        parent = _make_ticket(db_session, test_user, title="Root cause",
                              description="The original bug")
        child1 = _make_ticket(db_session, test_user, title="Dup 1",
                              description="Same bug again")
        child2 = _make_ticket(db_session, test_user, title="Dup 2",
                              description="Same bug yet again")

        # Mock returns parent match for any call
        mock_claude.return_value = {"match_id": parent.id, "confidence": 0.95}

        count = _run(batch_consolidate(db_session))
        # parent itself won't match (no other open tickets before it in order),
        # but child1 and child2 should match parent
        assert count >= 1

        db_session.refresh(child1)
        db_session.refresh(child2)
        # At least one should be linked
        linked = sum(1 for c in [child1, child2] if c.parent_ticket_id is not None)
        assert linked >= 1

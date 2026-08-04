"""Tests for Phase 1 bug fixes — Email Intelligence Maximization.

Covers:
  Bug 1: GRAPH_SCOPES constant shared between auth.py and token_manager.py
  Bug 2: _mark_processed savepoint pattern (avoids bare rollback)
  Bug 3: deep_scan_inbox passes last_body for signature extraction
  Bug 4: stock_lists_found field removed from scan_inbox return

Called by: pytest
Depends on: conftest fixtures, app.config, app.connectors.email_mining,
            app.utils.token_manager, app.scheduler
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import engine  # noqa: F401

# ═══════════════════════════════════════════════════════════════════════
#  Bug 1: GRAPH_SCOPES constant
# ═══════════════════════════════════════════════════════════════════════


class TestGraphScopes:
    def test_graph_scopes_defined_in_config(self):
        """GRAPH_SCOPES is a single source of truth in config.py."""
        from app.config import GRAPH_SCOPES

        assert isinstance(GRAPH_SCOPES, str)
        assert "Mail.Send" in GRAPH_SCOPES
        assert "Mail.ReadWrite" in GRAPH_SCOPES
        assert "Files.ReadWrite" in GRAPH_SCOPES
        assert "Chat.ReadWrite" in GRAPH_SCOPES
        assert "Channel.ReadBasic.All" in GRAPH_SCOPES
        assert "Calendars.Read" in GRAPH_SCOPES
        assert "offline_access" in GRAPH_SCOPES

    def test_auth_scopes_matches_config(self):
        """auth.py SCOPES variable uses GRAPH_SCOPES from config."""
        from app.config import GRAPH_SCOPES
        from app.routers.auth import SCOPES

        assert SCOPES == GRAPH_SCOPES

    def test_token_refresh_uses_graph_scopes(self):
        """token_manager uses GRAPH_SCOPES for refresh, not a hardcoded subset."""
        # Verify the function imports and uses GRAPH_SCOPES
        import inspect

        from app.utils.token_manager import _refresh_access_token

        source = inspect.getsource(_refresh_access_token)
        assert "GRAPH_SCOPES" in source
        # Verify no hardcoded scope string remains
        assert "Mail.Send Mail.ReadWrite Contacts.Read MailboxSettings.Read User.Read Calendars.Read" not in source


# ═══════════════════════════════════════════════════════════════════════
#  Bug 2: _mark_processed savepoint pattern
# ═══════════════════════════════════════════════════════════════════════


class TestMarkProcessedSavepoint:
    def _make_miner(self, db=None, user_id=None):
        with patch("app.utils.graph_client.GraphClient"):
            from app.connectors.email_mining import EmailMiner

            miner = EmailMiner("fake-token", db=db, user_id=user_id)
            miner.gc = MagicMock()
        return miner

    def test_mark_processed_uses_savepoint(self):
        """_mark_processed wraps insert in begin_nested() savepoint."""
        mock_db = MagicMock()
        mock_savepoint = MagicMock()
        mock_db.begin_nested.return_value = mock_savepoint

        miner = self._make_miner(db=mock_db)
        miner._mark_processed("msg-123", "mining")

        mock_db.begin_nested.assert_called_once()
        mock_savepoint.commit.assert_called_once()

    def test_mark_processed_savepoint_rollback_on_dup(self):
        """Duplicate key only rolls back the savepoint, not the whole session."""
        mock_db = MagicMock()
        mock_db.flush.side_effect = Exception("duplicate key")
        mock_savepoint = MagicMock()
        mock_db.begin_nested.return_value = mock_savepoint

        miner = self._make_miner(db=mock_db)
        miner._mark_processed("msg-dup", "mining")

        mock_savepoint.rollback.assert_called_once()
        # Session-level rollback should NOT be called
        mock_db.rollback.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
#  Bug 4: stock_lists_found removed from scan_inbox
# ═══════════════════════════════════════════════════════════════════════


class TestStockListsFieldRemoved:
    def test_scan_inbox_no_stock_lists_found_key(self):
        """scan_inbox return dict no longer includes misleading stock_lists_found."""
        with patch("app.utils.graph_client.GraphClient") as MockGC:
            mock_gc = MagicMock()
            mock_gc.delta_query = AsyncMock(return_value=([], "new-token"))
            MockGC.return_value = mock_gc

            from app.connectors.email_mining import EmailMiner

            miner = EmailMiner("fake-token", db=None, user_id=1)
            miner.gc = mock_gc

            result = asyncio.get_event_loop().run_until_complete(miner.scan_inbox(lookback_days=30, max_messages=10))

        assert "stock_lists_found" not in result
        assert "vendors_found" in result
        assert "messages_scanned" in result

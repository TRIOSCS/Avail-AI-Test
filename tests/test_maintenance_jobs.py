"""Tests for app/jobs/maintenance_jobs.py — _job_contact_dedup control flow.

Covers the mock-level control flow (no-dupes path, error rollback). The actual
merge behaviour — keeper selection, loser deletion, scalar backfill, and the
CRITICAL child-row reassignment (attachments/tasks must not cascade-delete) — is
covered against the real DB in test_maintenance_jobs_dedup_cascade.py (a mock
session can't exercise the real ORM cascade, which is how the cascade data-loss
bug originally slipped through).

Called by: pytest
Depends on: app.jobs.maintenance_jobs
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


def _build_mock_db(dupe_rows, contacts_by_group):
    """Build a mock DB session with controlled query chains.

    dupe_rows: list of group-by result rows
    contacts_by_group: list of list — one contact list per dupe_row
    """
    db = MagicMock()

    # Build contact-lookup chains (one per dupe group)
    contact_chains = []
    for contacts in contacts_by_group:
        c = MagicMock()
        c.filter.return_value = c
        c.order_by.return_value = c
        c.all.return_value = contacts
        contact_chains.append(c)

    # First .query() → dupe aggregation chain; subsequent → contact chains
    dupe_chain = MagicMock()
    dupe_chain.filter.return_value = dupe_chain
    dupe_chain.group_by.return_value = dupe_chain
    dupe_chain.having.return_value = dupe_chain
    dupe_chain.all.return_value = dupe_rows

    db.query.side_effect = [dupe_chain] + contact_chains

    return db


class TestJobContactDedup:
    """Control-flow tests for the _job_contact_dedup scheduler job."""

    @patch("app.database.SessionLocal")
    def test_no_dupes_no_changes(self, mock_session_local):
        """When no duplicates exist, no deletes or merges happen."""
        db = _build_mock_db([], [])
        mock_session_local.return_value = db

        from app.jobs.maintenance_jobs import _job_contact_dedup

        _run(_job_contact_dedup())

        db.delete.assert_not_called()
        db.commit.assert_called_once()
        db.close.assert_called_once()

    @patch("app.database.SessionLocal")
    def test_error_triggers_rollback(self, mock_session_local):
        """On exception, the session is rolled back, closed, and error re-raised."""
        db = MagicMock()
        dupe_chain = MagicMock()
        dupe_chain.filter.side_effect = RuntimeError("DB exploded")
        db.query.return_value = dupe_chain
        mock_session_local.return_value = db

        from app.jobs.maintenance_jobs import _job_contact_dedup

        with pytest.raises(RuntimeError, match="DB exploded"):
            _run(_job_contact_dedup())

        db.rollback.assert_called()
        db.close.assert_called_once()

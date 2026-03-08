"""Tests for app/jobs/maintenance_jobs.py — _job_contact_dedup function.

Covers: duplicate merging, no-dupes path, error rollback.
Called by: pytest
Depends on: app.jobs.maintenance_jobs
"""

import asyncio
from unittest.mock import MagicMock, patch


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


class _FakeContact:
    """Minimal stand-in for SiteContact with attribute tracking."""

    def __init__(
        self, id, customer_site_id, email, full_name=None, title=None, phone=None, notes=None, linkedin_url=None
    ):
        self.id = id
        self.customer_site_id = customer_site_id
        self.email = email
        self.full_name = full_name
        self.title = title
        self.phone = phone
        self.notes = notes
        self.linkedin_url = linkedin_url


class _FakeDupeRow:
    """Mimics a SQLAlchemy keyed-tuple result from the group-by query."""

    def __init__(self, customer_site_id, em, cnt):
        self.customer_site_id = customer_site_id
        self.em = em
        self.cnt = cnt


def _build_mock_db(dupe_rows, contacts_by_group):
    """Build a mock DB session with controlled query chains.

    dupe_rows: list of _FakeDupeRow for the group-by query
    contacts_by_group: list of list[_FakeContact] — one per dupe_row
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
    """Tests for the _job_contact_dedup scheduler job."""

    @patch("app.database.SessionLocal")
    def test_dedup_merges_and_deletes(self, mock_session_local):
        """Two contacts with same site+email: best keeps fields, loser deleted."""
        contact_a = _FakeContact(
            id=1,
            customer_site_id=10,
            email="alice@example.com",
            full_name="Alice Smith",
            phone="+15551234567",
        )
        contact_b = _FakeContact(
            id=2,
            customer_site_id=10,
            email="Alice@example.com",
            title="VP Sales",
            notes="Met at trade show",
        )
        dupe_row = _FakeDupeRow(customer_site_id=10, em="alice@example.com", cnt=2)

        db = _build_mock_db([dupe_row], [[contact_a, contact_b]])
        mock_session_local.return_value = db

        from app.jobs.maintenance_jobs import _job_contact_dedup

        _run(_job_contact_dedup())

        db.delete.assert_called_once()
        db.commit.assert_called_once()
        db.close.assert_called_once()

        # Verify the winner got the loser's fields merged
        deleted_contact = db.delete.call_args[0][0]
        if deleted_contact is contact_b:
            assert contact_a.title == "VP Sales"
            assert contact_a.notes == "Met at trade show"
        else:
            assert contact_b.full_name == "Alice Smith"
            assert contact_b.phone == "+15551234567"

    @patch("app.database.SessionLocal")
    def test_dedup_best_has_most_fields(self, mock_session_local):
        """Contact with more filled fields is kept as the winner."""
        winner = _FakeContact(
            id=1,
            customer_site_id=10,
            email="bob@example.com",
            full_name="Bob",
            title="CTO",
            phone="+15559999999",
            notes="Important",
            linkedin_url="https://linkedin.com/in/bob",
        )
        loser = _FakeContact(
            id=2,
            customer_site_id=10,
            email="bob@example.com",
            full_name="Robert",
        )
        dupe_row = _FakeDupeRow(customer_site_id=10, em="bob@example.com", cnt=2)
        db = _build_mock_db([dupe_row], [[winner, loser]])
        mock_session_local.return_value = db

        from app.jobs.maintenance_jobs import _job_contact_dedup

        _run(_job_contact_dedup())

        db.delete.assert_called_once_with(loser)
        db.commit.assert_called_once()

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
        """On exception, the session is rolled back and closed."""
        db = MagicMock()
        dupe_chain = MagicMock()
        dupe_chain.filter.side_effect = RuntimeError("DB exploded")
        db.query.return_value = dupe_chain
        mock_session_local.return_value = db

        from app.jobs.maintenance_jobs import _job_contact_dedup

        _run(_job_contact_dedup())

        db.rollback.assert_called()
        db.close.assert_called_once()

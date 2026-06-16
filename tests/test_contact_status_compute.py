"""test_contact_status_compute.py — Tests for the contact status auto-compute scheduler
job.

Covers:
- Active contact (activity ≤7 days) → status='active'
- Quiet contact (activity 30-90 days) → status='quiet'
- Inactive contact (activity >90 days) → status='inactive'
- Champion status is never downgraded
- No activity + new contact stays 'new'
- No activity + old contact → 'inactive'
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.models import ActivityLog
from app.models.crm import Company, CustomerSite, SiteContact


class _FakeSessionLocal:
    """Returns the test db_session when called as SessionLocal()."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self._session


class TestContactStatusCompute:
    def _make_contact(self, db, site_id, name, status="new", created_days_ago=0):
        sc = SiteContact(
            customer_site_id=site_id,
            full_name=name,
            contact_status=status,
            is_active=True,
            created_at=datetime.now(timezone.utc) - timedelta(days=created_days_ago),
        )
        db.add(sc)
        db.flush()
        return sc

    def _make_activity(self, db, user_id, contact_id, days_ago):
        al = ActivityLog(
            user_id=user_id,
            activity_type="email_sent",
            channel="email",
            site_contact_id=contact_id,
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        db.add(al)
        db.flush()
        return al

    def _make_site(self, db):
        co = Company(name="Test Co", is_active=True, created_at=datetime.now(timezone.utc))
        db.add(co)
        db.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ", created_at=datetime.now(timezone.utc))
        db.add(site)
        db.flush()
        return site

    def _run_job(self, db_session):
        from app.jobs.email_jobs import _job_contact_status_compute

        # Patch SessionLocal so the job uses our test session, and patch close() to no-op
        with patch("app.database.SessionLocal", _FakeSessionLocal(db_session)):
            db_session.close = lambda: None  # prevent job from closing our session
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_job_contact_status_compute())
            finally:
                loop.close()

    @pytest.mark.parametrize(
        ("name", "status", "created_days_ago", "activity_days_ago", "expected"),
        [
            pytest.param("Active Person", "new", 0, 3, "active", id="active_contact"),
            pytest.param("Quiet Person", "new", 0, 45, "quiet", id="quiet_contact"),
            pytest.param("Gone Person", "new", 0, 120, "inactive", id="inactive_contact"),
            pytest.param("Champion Person", "champion", 0, 120, "champion", id="champion_never_downgraded"),
            pytest.param("New Person", "new", 5, None, "new", id="no_activity_new_stays_new"),
            pytest.param("Old Person", "new", 120, None, "inactive", id="no_activity_old_goes_inactive"),
        ],
    )
    def test_status_compute(self, db_session, test_user, name, status, created_days_ago, activity_days_ago, expected):
        """The auto-compute job derives contact_status from recency, never downgrading
        champions."""
        site = self._make_site(db_session)
        sc = self._make_contact(db_session, site.id, name, status=status, created_days_ago=created_days_ago)
        if activity_days_ago is not None:
            self._make_activity(db_session, test_user.id, sc.id, days_ago=activity_days_ago)
        db_session.commit()

        self._run_job(db_session)
        db_session.refresh(sc)
        assert sc.contact_status == expected

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

    def test_active_contact(self, db_session, test_user):
        """Contact with activity ≤7 days ago → 'active'."""
        site = self._make_site(db_session)
        sc = self._make_contact(db_session, site.id, "Active Person")
        self._make_activity(db_session, test_user.id, sc.id, days_ago=3)
        db_session.commit()

        self._run_job(db_session)
        db_session.refresh(sc)
        assert sc.contact_status == "active"

    def test_quiet_contact(self, db_session, test_user):
        """Contact with activity 30-90 days ago → 'quiet'."""
        site = self._make_site(db_session)
        sc = self._make_contact(db_session, site.id, "Quiet Person")
        self._make_activity(db_session, test_user.id, sc.id, days_ago=45)
        db_session.commit()

        self._run_job(db_session)
        db_session.refresh(sc)
        assert sc.contact_status == "quiet"

    def test_inactive_contact(self, db_session, test_user):
        """Contact with activity >90 days ago → 'inactive'."""
        site = self._make_site(db_session)
        sc = self._make_contact(db_session, site.id, "Gone Person")
        self._make_activity(db_session, test_user.id, sc.id, days_ago=120)
        db_session.commit()

        self._run_job(db_session)
        db_session.refresh(sc)
        assert sc.contact_status == "inactive"

    def test_champion_never_downgraded(self, db_session, test_user):
        """Champion status is never changed by the auto-compute job."""
        site = self._make_site(db_session)
        sc = self._make_contact(db_session, site.id, "Champion Person", status="champion")
        self._make_activity(db_session, test_user.id, sc.id, days_ago=120)
        db_session.commit()

        self._run_job(db_session)
        db_session.refresh(sc)
        assert sc.contact_status == "champion"

    def test_no_activity_new_stays_new(self, db_session):
        """Contact created recently with no activity stays 'new'."""
        site = self._make_site(db_session)
        sc = self._make_contact(db_session, site.id, "New Person", created_days_ago=5)
        db_session.commit()

        self._run_job(db_session)
        db_session.refresh(sc)
        assert sc.contact_status == "new"

    def test_no_activity_old_goes_inactive(self, db_session):
        """Contact created >90 days ago with no activity → 'inactive'."""
        site = self._make_site(db_session)
        sc = self._make_contact(db_session, site.id, "Old Person", created_days_ago=120)
        db_session.commit()

        self._run_job(db_session)
        db_session.refresh(sc)
        assert sc.contact_status == "inactive"

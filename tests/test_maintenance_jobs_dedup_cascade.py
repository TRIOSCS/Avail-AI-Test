"""tests/test_maintenance_jobs_dedup_cascade.py — real-DB regression for the contact-
dedup cron's cascade data loss.

The existing tests/test_maintenance_jobs.py exercise _job_contact_dedup with a
mocked session + a fake SiteContact stand-in, so they never run the real ORM
cascade and missed that db.delete(loser) destroys the loser's attachments
(cascade='all, delete-orphan') and open tasks (FK ondelete=CASCADE). These tests
run against the real conftest SQLite engine (FK pragma ON) so the cascade fires.

Called by: pytest
Depends on: app.jobs.maintenance_jobs, app.services.contact_merge_service,
            app.models.crm/task, conftest.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

import app.database as appdb
from app.models.crm import Company, CustomerSite, SiteContact, SiteContactAttachment
from app.models.task import RequisitionTask

_run = asyncio.run


@pytest.fixture()
def _bind_job_session(db_session: Session, monkeypatch):
    """Point the job's SessionLocal() at the same test engine db_session uses, so the
    job's own session shares the StaticPool connection (and its commits are visible to
    the assertions)."""
    job_sessionmaker = sessionmaker(bind=db_session.get_bind(), autoflush=False)
    monkeypatch.setattr(appdb, "SessionLocal", job_sessionmaker)
    return job_sessionmaker


def _dupe_pair(db_session: Session) -> tuple[SiteContact, SiteContact]:
    """A keeper (more complete) + loser sharing one site and email — the exact shape
    _job_contact_dedup groups on."""
    co = Company(name="Dedup Cascade Co", is_active=True)
    db_session.add(co)
    db_session.commit()
    site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.commit()

    # The (customer_site_id, email) unique constraint is exact-case; the cron dedups
    # on lower(email), so real dupes differ only by email case.
    keeper = SiteContact(
        customer_site_id=site.id, full_name="Keep Me", email="dup@example.com", title="Director", phone="+15550000001"
    )
    loser = SiteContact(customer_site_id=site.id, full_name="Lose Me", email="DUP@example.com")
    db_session.add_all([keeper, loser])
    db_session.commit()
    db_session.refresh(keeper)
    db_session.refresh(loser)
    return keeper, loser


def test_dedup_preserves_loser_attachment_reassigned_to_keeper(db_session: Session, _bind_job_session):
    """The loser's attachment must survive dedup and be reassigned to the keeper, not
    cascade-deleted."""
    keeper, loser = _dupe_pair(db_session)
    att = SiteContactAttachment(site_contact_id=loser.id, file_name="invoice.pdf", created_at=datetime.now(UTC))
    db_session.add(att)
    db_session.commit()
    att_id, keeper_id, loser_id = att.id, keeper.id, loser.id

    from app.jobs.maintenance_jobs import _job_contact_dedup

    _run(_job_contact_dedup())

    db_session.expire_all()
    assert db_session.get(SiteContact, loser_id) is None, "loser should be merged away"
    assert db_session.get(SiteContact, keeper_id) is not None, "keeper must survive"
    surviving = db_session.get(SiteContactAttachment, att_id)
    assert surviving is not None, "attachment must NOT be cascade-deleted"
    assert surviving.site_contact_id == keeper_id, "attachment must be reassigned to the keeper"


def test_dedup_preserves_loser_task_reassigned_to_keeper(db_session: Session, _bind_job_session):
    """The loser's open task must survive dedup and be reassigned to the keeper, not
    cascade-deleted."""
    keeper, loser = _dupe_pair(db_session)
    task = RequisitionTask(
        site_contact_id=loser.id,
        title="Follow up call",
        task_type="general",
        status="todo",
        created_at=datetime.now(UTC),
    )
    db_session.add(task)
    db_session.commit()
    task_id, keeper_id = task.id, keeper.id

    from app.jobs.maintenance_jobs import _job_contact_dedup

    _run(_job_contact_dedup())

    db_session.expire_all()
    surviving = db_session.get(RequisitionTask, task_id)
    assert surviving is not None, "task must NOT be cascade-deleted"
    assert surviving.site_contact_id == keeper_id, "task must be reassigned to the keeper"


def test_dedup_keeps_most_complete_deletes_loser_and_backfills(db_session: Session, _bind_job_session):
    """The more-complete contact is kept, the loser deleted, and the loser's non-null
    fields backfill gaps on the keeper.

    (Real-DB replacement for the old mock-based test_dedup_merges_and_deletes /
    _best_has_most_fields.)
    """
    keeper, loser = _dupe_pair(db_session)  # keeper has 3 merge-fields, loser has 1
    loser.linkedin_url = "https://linkedin.com/in/lose-me"  # a gap the keeper lacks
    db_session.commit()
    keeper_id, loser_id = keeper.id, loser.id

    from app.jobs.maintenance_jobs import _job_contact_dedup

    _run(_job_contact_dedup())

    db_session.expire_all()
    assert db_session.get(SiteContact, loser_id) is None, "loser deleted"
    survivor = db_session.get(SiteContact, keeper_id)
    assert survivor is not None, "most-complete contact kept"
    assert survivor.linkedin_url == "https://linkedin.com/in/lose-me", "loser's field backfilled onto keeper"

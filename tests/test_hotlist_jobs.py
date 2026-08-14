"""tests/test_hotlist_jobs.py — the flag-OFF weekly hotlist re-search job.

Covers: registration gating on `hotlist_research_enabled` (explicit-True only,
mirroring the teams-push pattern), and the job body's selection/dedup/cap
behavior against the ics/nc/tbf enqueue wrappers (mocked — no browser workers).

Called by: pytest
Depends on: app/jobs/hotlist_jobs.py, conftest fixtures.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from app.jobs.hotlist_jobs import _job_hotlist_research, register_hotlist_jobs
from app.models import Requirement, Requisition
from tests.conftest import engine  # noqa: F401


class TestRegistration:
    def _register(self, *, enabled):
        mock_scheduler = MagicMock()
        mock_settings = MagicMock()
        mock_settings.hotlist_research_enabled = enabled
        register_hotlist_jobs(mock_scheduler, mock_settings)
        return [c.kwargs.get("id") for c in mock_scheduler.add_job.call_args_list]

    def test_registered_when_flag_on(self):
        assert "hotlist_research" in self._register(enabled=True)

    def test_absent_when_flag_off(self):
        assert "hotlist_research" not in self._register(enabled=False)

    def test_absent_on_magicmock_settings(self):
        # `is True` check: a MagicMock attribute (truthy but not True) must not
        # register a spurious job in unrelated scheduler tests.
        mock_scheduler = MagicMock()
        register_hotlist_jobs(mock_scheduler, MagicMock())
        assert "hotlist_research" not in [c.kwargs.get("id") for c in mock_scheduler.add_job.call_args_list]


async def test_job_enqueues_hotlist_demand_deduped(db_session, test_user, monkeypatch):
    """Part-level AND requisition-level hotlist MPNs are enqueued once each through all
    three connectors; non-hotlist parts are not."""
    open_req = Requisition(name="open deal", status="open", created_by=test_user.id)
    hot_req = Requisition(name="hot deal", status="hotlist", created_by=test_user.id)
    won_req = Requisition(name="won deal", status="won", created_by=test_user.id)
    db_session.add_all([open_req, hot_req, won_req])
    db_session.flush()
    # Part-level hotlist on the won deal + two duplicate-MPN rows (dedup) +
    # a requisition-level hotlist part + a plain open part (excluded).
    db_session.add_all(
        [
            Requirement(requisition_id=won_req.id, primary_mpn="HOTJOB-1", sourcing_status="hotlist"),
            Requirement(requisition_id=open_req.id, primary_mpn="HOTJOB-1", sourcing_status="hotlist"),
            Requirement(requisition_id=hot_req.id, primary_mpn="HOTJOB-2", sourcing_status="open"),
            Requirement(requisition_id=open_req.id, primary_mpn="HOTJOB-3", sourcing_status="open"),
        ]
    )
    db_session.commit()

    calls: list[tuple[str, int]] = []

    def _rec(name):
        def _f(requirement_id, db, **kwargs):
            calls.append((name, requirement_id))

        return _f

    with (
        patch("app.services.ics_worker.queue_manager.enqueue_for_ics_search", side_effect=_rec("ics")),
        patch("app.services.nc_worker.queue_manager.enqueue_for_nc_search", side_effect=_rec("nc")),
        patch("app.services.tbf_worker.queue_manager.enqueue_for_tbf_search", side_effect=_rec("tbf")),
        patch("app.jobs.hotlist_jobs.SessionLocal", create=True) as _,
        patch("app.database.SessionLocal", return_value=db_session),
    ):
        # SessionLocal is imported inside the job — patch the source module and
        # neutralize close() so the shared test session survives.
        monkeypatch.setattr(db_session, "close", lambda: None)
        await _job_hotlist_research()

    enqueued_reqs = {rid for _, rid in calls}
    hot_part_ids = {
        r.id for r in db_session.query(Requirement).filter(Requirement.primary_mpn.in_(["HOTJOB-1", "HOTJOB-2"])).all()
    }
    open_part_id = db_session.query(Requirement).filter_by(primary_mpn="HOTJOB-3").first().id
    # One requirement per distinct MPN (dedup collapsed the duplicate HOTJOB-1),
    # each fanned out to 3 connectors; the plain open part never enqueued.
    assert len(enqueued_reqs) == 2
    assert enqueued_reqs <= hot_part_ids
    assert open_part_id not in enqueued_reqs
    assert len(calls) == 6


async def test_job_respects_cap(db_session, test_user, monkeypatch):
    """hotlist_research_max_parts caps distinct MPNs per run (oldest-searched first)."""
    req = Requisition(name="hot deal", status="hotlist", created_by=test_user.id)
    db_session.add(req)
    db_session.flush()
    now = datetime.now(UTC)
    for i in range(3):
        db_session.add(
            Requirement(
                requisition_id=req.id,
                primary_mpn=f"CAP-{i}",
                sourcing_status="hotlist",
                last_searched_at=now - timedelta(days=i),
            )
        )
    db_session.commit()

    calls: list[int] = []
    with (
        patch(
            "app.services.ics_worker.queue_manager.enqueue_for_ics_search",
            side_effect=lambda r, d, **k: calls.append(r),
        ),
        patch("app.services.nc_worker.queue_manager.enqueue_for_nc_search", side_effect=lambda r, d, **k: None),
        patch("app.services.tbf_worker.queue_manager.enqueue_for_tbf_search", side_effect=lambda r, d, **k: None),
        patch("app.database.SessionLocal", return_value=db_session),
        patch("app.config.settings.hotlist_research_max_parts", 1),
    ):
        monkeypatch.setattr(db_session, "close", lambda: None)
        await _job_hotlist_research()

    assert len(calls) == 1
    # Oldest last_searched_at first → CAP-2 (searched 2 days ago) wins the slot.
    picked = db_session.get(Requirement, calls[0])
    assert picked.primary_mpn == "CAP-2"

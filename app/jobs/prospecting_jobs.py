"""Prospecting background jobs — SP4 account sweep (+ parked reactivation).

W1 simplification (2026-08-04, docs/W1_JOB_DISPOSITION.md):
- DELETED: the 6 Explorium discovery-machine monthly jobs (pool_health_report,
  discover_prospects, enrich_pool, find_contacts, refresh_scores,
  expire_and_resurface) and their implementation module
  app/services/prospect_scheduler.py.
- PARKED: auto_surface_reactivation — its gate defaulted ON, so the
  registration call was removed; the implementation below stays.
  Comeback trigger: team exists (spec §5.4).
- PARKED (no change): account_sweep — already gated off by default via
  account_sweep_enabled=False. Comeback trigger: team exists (spec §5.4).

Called by: app/jobs/__init__.py via register_sweep_jobs()
Depends on: app.services.prospect_reclamation
"""

from apscheduler.triggers.cron import CronTrigger

from ..scheduler import _traced_job


def register_sweep_jobs(scheduler, settings):
    """Register the SP4 account-sweep job (reactivation registration parked in W1)."""
    if settings.prospecting_enabled and settings.account_sweep_enabled:
        scheduler.add_job(
            _job_account_sweep,
            CronTrigger(hour=1, minute=0),
            id="account_sweep",
            name=f"{settings.account_sweep_inactivity_days}-day account hardline sweep",
        )


@_traced_job
async def _job_account_sweep():
    """Daily 1AM — sweep dormant owned accounts into prospecting pool."""
    from ..services.prospect_reclamation import job_account_sweep

    await job_account_sweep()


@_traced_job
async def _job_auto_surface_reactivation():
    """Surface unassigned past customers (registration parked in W1; spec §5.4)."""
    from ..services.prospect_reclamation import job_auto_surface_reactivation

    await job_auto_surface_reactivation()

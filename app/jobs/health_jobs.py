"""API health monitoring background jobs — none scheduled since W1 simplification
(2026-08-04).

All four health jobs were removed per docs/W1_JOB_DISPOSITION.md (spec §3 kernel
list; §5.5 keys-off shown honestly, not polled): health_ping, health_deep,
cleanup_usage_log, reset_monthly_usage. The health-check fan-out stays
code-complete in app/services/health_monitor.py (run_health_checks); git history
restores the pollers when connector keys go on (§5.1).

Called by: app/jobs/__init__.py via register_health_jobs()
Depends on: nothing (registers no jobs)
"""


def register_health_jobs(scheduler, settings):
    """Register API health monitoring jobs with the scheduler — none since W1 (no-
    op)."""

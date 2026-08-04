"""Maintenance background jobs — none scheduled since W1 simplification (2026-08-04).

All six maintenance jobs were removed per docs/W1_JOB_DISPOSITION.md (spec §3:
scheduler == kernel list): cache_cleanup (intel_cache empty; Redis TTL-checks at
read time), auto_attribute_activities + auto_dedup (AI background machinery; the
manual merge path stays), reset_connector_errors + integrity_check (the on-demand
/api/admin integrity route keeps serving run_integrity_check), contact_dedup
(zero duplicate groups left). Git history restores any of them.

Called by: app/jobs/__init__.py via register_maintenance_jobs()
Depends on: nothing (registers no jobs)
"""


def register_maintenance_jobs(scheduler, settings):
    """Register maintenance jobs with the scheduler — none since W1 (no-op)."""

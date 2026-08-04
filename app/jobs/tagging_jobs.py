"""Material tagging background jobs — none remain (W1 simplification, 2026-08-04).

All five scheduled tagging jobs were removed per docs/W1_JOB_DISPOSITION.md:
  - internal_confidence_boost, sighting_mining: DELETED (zero yield)
  - prefix_backfill: on-demand via app/management/prefix_backfill.py
  - ai_tagging: on-demand via app/management/ai_tagging.py (AI keys required)
  - spec_enrichment: on-demand via app/management/enrich_specs.py

Called by: app/jobs/__init__.py via register_tagging_jobs()
Depends on: nothing (registration is a no-op)
"""


def register_tagging_jobs(scheduler, settings):
    """Register tagging jobs with the scheduler — intentionally registers none.

    Kept as a no-op seam for app/jobs/__init__.py; delete both together once the caller
    drops the import. The tagging suite runs on-demand only (see module docstring for
    the management commands).
    """

"""Background jobs for the Knowledge Ledger — none scheduled since W1 simplification
(2026-08-04).

The knowledge_expire_stale job (log-only count of expired entries; expiry is
applied at query time, not by the job) was removed per docs/W1_JOB_DISPOSITION.md
(spec §8 DELETE list — Dashboard + Knowledge pages). The KB-insight refresh job
(`knowledge_refresh_insights`) was deleted earlier, 2026-07-06. Both are
recoverable from git history.

Called by: app/jobs/__init__.py via register_knowledge_jobs()
Depends on: nothing (registers no jobs)
"""


def register_knowledge_jobs(scheduler, settings):
    """Register knowledge ledger background jobs — none since W1 (no-op)."""

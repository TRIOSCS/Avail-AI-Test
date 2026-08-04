"""Selective auto-task scheduler — none scheduled since W1 simplification (2026-08-04).

The bid_due_alerts job (daily 'Bid due' tasks for requisitions approaching a
parseable deadline) was DELETED per docs/W1_JOB_DISPOSITION.md (spec §3: not on
the kernel job list; 2 tasks ever created). Its task_service hook
(``on_bid_due_soon``) went with it. Git history restores both.

The useful task triggers (new requirements, buy plan assignments, email-parsed
offers, new offers) fire inline from their respective service/router hooks —
not from the scheduler — and are unaffected.

Called by: app/jobs/__init__.py via register_task_jobs()
Depends on: nothing (registers no jobs)
"""


def register_task_jobs(scheduler, settings):
    """Register task auto-generation jobs — none since W1 (no-op)."""

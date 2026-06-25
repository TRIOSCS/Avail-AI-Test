"""TasksActionSource — ACTION alert for open tasks assigned to ME.

Surfaces the count of open (not-done) tasks assigned to the current user, driving the
My Day nav badge. Wires the already-built ``task_service.get_my_tasks_summary`` /
``get_my_tasks`` helpers into the cross-app AlertSource registry.

As an ACTION source the count derives PURELY from work-state: it does NOT subtract
seen_ref_ids. ``alert_seen`` only gates the cosmetic one-time in-tab spotlight pulse —
a task leaves the count only when it is completed. Hence ``count_for_user`` ==
``len(new_items_for_user(...))`` for a user whose open tasks all carry distinct ids.

The count is the authoritative ``assigned_to_me`` figure from get_my_tasks_summary (one
aggregate query); the items are built over get_my_tasks (the row helper) so the spotlight
anchors line up with the My Day task rows (``my-day-task-{id}``).

Called by: services/alerts/sources/__init__.py (registered under the 'my-day' tab).
Depends on: services/alerts/base.AlertSource, constants.AlertKind,
            services/task_service.{get_my_tasks_summary,get_my_tasks}.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import AlertKind
from app.models.auth import User
from app.services.task_service import get_my_tasks, get_my_tasks_summary

from ..base import AlertItem, AlertSource, Temperament


class TasksActionSource(AlertSource):
    """Open tasks assigned to the current user (ACTION)."""

    key = "tasks"
    kind = AlertKind.TASKS_ACTION
    temperament = Temperament.ACTION

    def count_for_user(self, db: Session, user: User) -> int:
        """The open-tasks-assigned-to-me count (the summary's ``assigned_to_me``)."""
        return get_my_tasks_summary(db, user.id)["assigned_to_me"]

    def new_items_for_user(self, db: Session, user: User) -> list[AlertItem]:
        """One AlertItem per open task assigned to me, anchored to its My Day row."""
        return [AlertItem(ref_id=t.id, anchor=f"my-day-task-{t.id}") for t in get_my_tasks(db, user.id)]

"""Tests for TasksActionSource — the "open tasks assigned to ME" ACTION alert.

Mirrors the BuyplanActionSource test: count_for_user returns the open-assigned count,
new_items_for_user returns one item per open task, seen does not change the count
(ACTION temperament), and the badge endpoint /v2/partials/alerts/my-day/badge renders
the count for a user with open tasks. Also asserts the source is registered under the
'my-day' tab.

Depends on: services/alerts/sources/tasks.TasksActionSource,
            services/alerts/base.record_seen, conftest fixtures.
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.constants import AlertKind, TaskStatus
from app.models.task import RequisitionTask
from app.services.alerts.base import record_seen
from app.services.alerts.sources.tasks import TasksActionSource

SOURCE = TasksActionSource()


@pytest.fixture()
def my_open_task(db_session: Session, test_user, test_requisition) -> RequisitionTask:
    """An open (todo) task assigned to test_user."""
    t = RequisitionTask(
        requisition_id=test_requisition.id,
        title="Source LM317T",
        status=TaskStatus.TODO.value,
        assigned_to_id=test_user.id,
        due_at=datetime.now(timezone.utc) - timedelta(days=1),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


# ── count ───────────────────────────────────────────────────────────────


def test_open_task_counts(db_session, test_user, my_open_task):
    assert SOURCE.count_for_user(db_session, test_user) == 1


def test_no_tasks_count_zero(db_session, test_user):
    assert SOURCE.count_for_user(db_session, test_user) == 0


def test_done_task_not_counted(db_session, test_user, test_requisition):
    db_session.add(
        RequisitionTask(
            requisition_id=test_requisition.id,
            title="Already done",
            status=TaskStatus.DONE.value,
            assigned_to_id=test_user.id,
            completed_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    assert SOURCE.count_for_user(db_session, test_user) == 0


def test_other_users_task_not_counted(db_session, test_user, manager_user, test_requisition):
    db_session.add(
        RequisitionTask(
            requisition_id=test_requisition.id,
            title="Manager's task",
            status=TaskStatus.TODO.value,
            assigned_to_id=manager_user.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    assert SOURCE.count_for_user(db_session, test_user) == 0


# ── items ─────────────────────────────────────────────────────────────────


def test_new_items_returns_open_tasks(db_session, test_user, my_open_task):
    items = SOURCE.new_items_for_user(db_session, test_user)
    assert [i.ref_id for i in items] == [my_open_task.id]
    # anchor lines up with the My Day task-row id so the spotlight can find it.
    assert items[0].anchor == f"my-day-task-{my_open_task.id}"


def test_count_equals_items_len(db_session, test_user, my_open_task, test_requisition):
    db_session.add(
        RequisitionTask(
            requisition_id=test_requisition.id,
            title="Second task",
            status=TaskStatus.TODO.value,
            assigned_to_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    assert SOURCE.count_for_user(db_session, test_user) == len(SOURCE.new_items_for_user(db_session, test_user))


# ── ACTION ignores seen ────────────────────────────────────────────────────


def test_seen_does_not_change_count(db_session, test_user, my_open_task):
    assert SOURCE.count_for_user(db_session, test_user) == 1
    record_seen(db_session, test_user, AlertKind.TASKS_ACTION, my_open_task.id)
    # ACTION temperament: seen only gates the cosmetic pulse, never the count.
    assert SOURCE.count_for_user(db_session, test_user) == 1
    assert [i.ref_id for i in SOURCE.new_items_for_user(db_session, test_user)] == [my_open_task.id]


# ── registry wiring ────────────────────────────────────────────────────────


def test_source_registered_under_my_day_tab():
    from app.services.alerts import tab_for_kind

    assert tab_for_kind(AlertKind.TASKS_ACTION) == "my-day"


def test_sources_for_my_day_tab_contains_tasks_source():
    from app.services.alerts import sources_for_tab

    kinds = [s.kind for s in sources_for_tab("my-day")]
    assert AlertKind.TASKS_ACTION in kinds


# ── badge endpoint ─────────────────────────────────────────────────────────


def test_my_day_badge_shows_count(client, my_open_task):
    """GET /v2/partials/alerts/my-day/badge returns the open-task count for the user."""
    resp = client.get("/v2/partials/alerts/my-day/badge")
    assert resp.status_code == 200
    assert ">1</span>" in resp.text


def test_my_day_badge_empty_when_no_tasks(client):
    resp = client.get("/v2/partials/alerts/my-day/badge")
    assert resp.status_code == 200
    assert resp.text == ""


def test_my_day_seen_returns_oob_nav_badge(client):
    """POST /v2/partials/alerts/tasks_action/seen returns OOB span id='my-day-nav-
    badge'.

    ref_ids='0' (no matching row — handler is fail-quiet) keeps the DB clean.
    """
    resp = client.post("/v2/partials/alerts/tasks_action/seen", data={"ref_ids": "0"})
    assert resp.status_code == 200
    assert 'id="my-day-nav-badge"' in resp.text

"""Tests for part-level comms-tab tasks and requisition status filtering.

Covers: part comms-tab task create/complete (HTMX partials) and the
GET /api/requisitions status filter.

Called by: pytest
Depends on: routers/htmx/requisitions.py, routers/requisitions/core.py, conftest fixtures
"""

from datetime import UTC, datetime, timedelta

# ── Helpers ─────────────────────────────────────────────────────────


def _first_requirement(db_session, requisition):
    """Return the first Requirement row for the given requisition."""
    from app.models import Requirement

    return db_session.query(Requirement).filter(Requirement.requisition_id == requisition.id).first()


def test_create_part_task_stores_aware_due_datetime(client, test_requisition, db_session):
    """POST /v2/partials/parts/{id}/tasks parses the <input type=date> string into a
    real aware UTC datetime before it binds to the timestamptz due_at column.

    Regression guard for H1: previously the bare 'YYYY-MM-DD' string was bound straight
    through UTCDateTime (unnormalized instant on PostgreSQL, AttributeError on read under
    SQLite). If the string-bind ever returns, due_at is a str and these assertions fail
    loudly.
    """
    from app.models.task import RequisitionTask

    req = _first_requirement(db_session, test_requisition)
    resp = client.post(
        f"/v2/partials/parts/{req.id}/tasks",
        data={"title": "Chase datasheet", "due_date": "2026-07-10"},
    )
    assert resp.status_code == 200

    db_session.expire_all()  # force a DB round-trip through UTCDateTime.process_result_value
    task = (
        db_session.query(RequisitionTask)
        .filter(RequisitionTask.requirement_id == req.id, RequisitionTask.title == "Chase datasheet")
        .one()
    )
    assert isinstance(task.due_at, datetime)  # NOT a raw string
    assert task.due_at.tzinfo is not None  # aware
    assert task.due_at.utcoffset() == timedelta(0)  # normalized to UTC
    assert task.due_at.date().isoformat() == "2026-07-10"


def test_create_part_task_empty_due_is_none(client, test_requisition, db_session):
    """An empty due_date stores NULL, not an empty string."""
    from app.models.task import RequisitionTask

    req = _first_requirement(db_session, test_requisition)
    resp = client.post(
        f"/v2/partials/parts/{req.id}/tasks",
        data={"title": "No due part task", "due_date": ""},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    task = (
        db_session.query(RequisitionTask)
        .filter(RequisitionTask.requirement_id == req.id, RequisitionTask.title == "No due part task")
        .one()
    )
    assert task.due_at is None


def test_create_part_task_malformed_due_422(client, test_requisition, db_session):
    """A malformed due_date is rejected with 422 rather than silently mis-stored."""
    req = _first_requirement(db_session, test_requisition)
    resp = client.post(
        f"/v2/partials/parts/{req.id}/tasks",
        data={"title": "Bad due part task", "due_date": "not-a-date"},
    )
    assert resp.status_code == 422


# ── Part comms-tab task completion — completion_note capture (M3) ─────


def test_mark_part_task_done_stores_completion_note(client, test_requisition, db_session, test_user):
    """POST /v2/partials/parts/tasks/{id}/done captures the optional completion_note.

    Regression guard for M3: the comms-tab "Mark done" control hx-includes an optional
    resolution note; the endpoint must persist it (previously completion_note was hard-
    coded to "" on every completion path, so the model field was write-only dead weight).
    """
    from app.models.task import RequisitionTask

    req = _first_requirement(db_session, test_requisition)
    # Create the task assigned to the completing user (only the assignee may complete).
    client.post(
        f"/v2/partials/parts/{req.id}/tasks",
        data={"title": "Confirm stock", "assigned_to": str(test_user.id)},
    )
    task = (
        db_session.query(RequisitionTask)
        .filter(RequisitionTask.requirement_id == req.id, RequisitionTask.title == "Confirm stock")
        .one()
    )
    resp = client.post(
        f"/v2/partials/parts/tasks/{task.id}/done",
        data={"completion_note": "Vendor confirmed 500 pcs in stock"},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    task = db_session.get(RequisitionTask, task.id)
    assert task.status == "done"
    assert task.completion_note == "Vendor confirmed 500 pcs in stock"


def test_mark_part_task_done_without_note_ok(client, test_requisition, db_session, test_user):
    """Completing without a note still works (note stays empty, task completes)."""
    from app.models.task import RequisitionTask

    req = _first_requirement(db_session, test_requisition)
    client.post(
        f"/v2/partials/parts/{req.id}/tasks",
        data={"title": "No-note task", "assigned_to": str(test_user.id)},
    )
    task = (
        db_session.query(RequisitionTask)
        .filter(RequisitionTask.requirement_id == req.id, RequisitionTask.title == "No-note task")
        .one()
    )
    resp = client.post(f"/v2/partials/parts/tasks/{task.id}/done")
    assert resp.status_code == 200

    db_session.expire_all()
    task = db_session.get(RequisitionTask, task.id)
    assert task.status == "done"
    assert task.completion_note == ""


# ── Part comms-tab date rendering — shared helper + unified format (L5) ─────


def test_comms_tab_renders_overdue_task_via_shared_helper(client, test_requisition, db_session, test_user):
    """The comms tab renders an overdue part task through the shared task_due_state
    helper and the ONE task-row date format (|fmtdate('%b %-d')).

    Regression guard for L5: the tab used to do `task.due_at.date() < today` in-template
    (raised AttributeError on a string due_at) and rendered the date as %m/%d/%y — diverging
    from the My-Day / requisition-tab formats. It now routes through task_due_state (no
    in-template datetime math) and the unified compact format.
    """
    from app.models.task import RequisitionTask

    req = _first_requirement(db_session, test_requisition)
    task = RequisitionTask(
        requisition_id=req.requisition_id,
        requirement_id=req.id,
        title="Overdue comms task",
        task_type="general",
        status="open",
        source="manual",
        created_by=test_user.id,
        due_at=datetime(2020, 1, 15, tzinfo=UTC),
    )
    db_session.add(task)
    db_session.commit()

    resp = client.get(f"/v2/partials/parts/{req.id}/tab/comms")
    assert resp.status_code == 200
    body = resp.text
    # Unified compact format ("Jan 15"), NOT the old "%m/%d/%y" ("01/15/20").
    assert "Jan 15" in body
    assert "01/15/20" not in body
    # Overdue styling applied via task_due_state (rose text on the due span).
    assert "text-rose-500" in body


# ── Requisition Status Filters ─────────────────────────────────────


class TestRequisitionStatusFilter:
    def _get_requisitions(self, resp):
        """Extract requisition list from response, handling various formats."""
        import json

        data = resp.json()
        # Handle double-encoded JSON (cached_endpoint may return a string)
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, list):
            return data
        return data.get("requisitions", data.get("items", []))

    def test_comma_separated_status_filter(self, client, db_session, test_user):
        """GET /api/requisitions?status=won,lost returns only matching statuses."""
        from app.models import Requisition

        r1 = Requisition(name="Won Req", status="won", created_by=test_user.id)
        r2 = Requisition(name="Lost Req", status="lost", created_by=test_user.id)
        r3 = Requisition(name="Active Req", status="open", created_by=test_user.id)
        db_session.add_all([r1, r2, r3])
        db_session.commit()

        resp = client.get("/api/requisitions?status=won,lost")
        assert resp.status_code == 200
        items = self._get_requisitions(resp)
        names = [r["name"] for r in items]
        assert "Won Req" in names
        assert "Lost Req" in names
        assert "Active Req" not in names

    def test_single_status_still_works(self, client, db_session, test_user):
        """GET /api/requisitions?status=draft still works with a single value."""
        from app.models import Requisition

        r = Requisition(name="Draft Filter Req", status="draft", created_by=test_user.id)
        db_session.add(r)
        db_session.commit()

        resp = client.get("/api/requisitions?status=draft")
        assert resp.status_code == 200
        items = self._get_requisitions(resp)
        names = [r["name"] for r in items]
        assert "Draft Filter Req" in names

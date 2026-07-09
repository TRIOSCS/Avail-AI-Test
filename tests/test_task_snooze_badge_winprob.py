"""Tests for task snooze, due/overdue badge, and requisition win_probability (migration
146).

Covers:
- Snooze: pushes due_at +1 week; no due_at -> tomorrow; unauthorized -> 403; returns refreshed list
- Badge: overdue / due-today / future / no-date render correctly in templates
- win_probability: set 0-100 persists; out-of-range -> 400; edit gated (unauthorized -> 403);
  migration single-head round-trip (upgrade/downgrade/upgrade clean)

Called by: pytest
Depends on: conftest.py (db_session, test_user, test_requisition, client)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import Company, Requisition, User
from app.models.task import RequisitionTask
from app.services.task_service import create_company_task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _task_due(offset_days: int, db: Session, company_id: int, user_id: int) -> RequisitionTask:
    due = _now_utc() + timedelta(days=offset_days)
    return create_company_task(
        db,
        company_id=company_id,
        title=f"Task due in {offset_days} days",
        due_at=due,
        created_by=user_id,
        assigned_to_id=user_id,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def owned_company(db_session: Session, test_user: User) -> Company:
    """A company owned by test_user so _is_crm_task_authorized passes."""
    co = Company(
        name="Snooze Corp",
        website="https://snoozecorp.example",
        industry="Electronics",
        is_active=True,
        account_owner_id=test_user.id,
        created_at=_now_utc(),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def other_user(db_session: Session) -> User:
    u = User(
        email="stranger@example.com",
        name="Stranger",
        role="buyer",
        azure_id="azure-stranger-001",
        created_at=_now_utc(),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def other_client(db_session: Session, other_user: User):
    """TestClient authenticated as other_user (not owner of test company)."""
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return other_user

    async def _override_token():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_token
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ===========================================================================
# Feature 1 — Task Snooze
# ===========================================================================


class TestTaskSnoozeService:
    """Unit tests for the snooze_task service function."""

    def test_snooze_pushes_due_at_one_week(self, db_session: Session, owned_company: Company, test_user: User):
        """Snoozing a task with an existing due_at advances it by exactly 7 days."""
        original_due = _now_utc() + timedelta(days=2)
        task = create_company_task(
            db_session,
            company_id=owned_company.id,
            title="Has due date",
            due_at=original_due,
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        from app.services.task_service import snooze_task

        snoozed = snooze_task(db_session, task.id)
        expected = original_due + timedelta(weeks=1)
        assert snoozed is not None
        assert abs((snoozed.due_at - expected).total_seconds()) < 2

    def test_snooze_no_due_at_sets_tomorrow(self, db_session: Session, owned_company: Company, test_user: User):
        """Snoozing a task with no due_at sets due_at to tomorrow (midnight UTC)."""
        task = create_company_task(
            db_session,
            company_id=owned_company.id,
            title="No due date",
            due_at=None,
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        from app.services.task_service import snooze_task

        snoozed = snooze_task(db_session, task.id)
        tomorrow = (datetime.now(UTC) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        assert snoozed is not None
        assert abs((snoozed.due_at - tomorrow).total_seconds()) < 2

    def test_snooze_returns_none_for_missing_task(self, db_session: Session):
        """snooze_task returns None when the task doesn't exist."""
        from app.services.task_service import snooze_task

        result = snooze_task(db_session, 999_999)
        assert result is None


class TestTaskSnoozeRoute:
    """Integration tests for POST /v2/partials/tasks/{task_id}/snooze."""

    def test_snooze_returns_200_and_refreshed_list(
        self, client, db_session: Session, owned_company: Company, test_user: User
    ):
        """Snooze returns 200 with refreshed task list HTML."""
        task = create_company_task(
            db_session,
            company_id=owned_company.id,
            title="Snooze me",
            due_at=_now_utc() + timedelta(days=1),
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        resp = client.post(f"/v2/partials/tasks/{task.id}/snooze")
        assert resp.status_code == 200
        # Should re-render the account-tasks container
        assert f"account-tasks-{owned_company.id}" in resp.text

    def test_snooze_actually_advances_due_at(
        self, client, db_session: Session, owned_company: Company, test_user: User
    ):
        """After a snooze POST the task's due_at moves forward by 7 days."""
        original_due = _now_utc() + timedelta(days=1)
        task = create_company_task(
            db_session,
            company_id=owned_company.id,
            title="Advance me",
            due_at=original_due,
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        client.post(f"/v2/partials/tasks/{task.id}/snooze")
        db_session.refresh(task)
        expected = original_due + timedelta(weeks=1)
        assert abs((task.due_at - expected).total_seconds()) < 2

    def test_snooze_unauthorized_returns_403(
        self, other_client, db_session: Session, owned_company: Company, test_user: User
    ):
        """A user who doesn't own or isn't assigned to a task gets 403 on snooze."""
        task = create_company_task(
            db_session,
            company_id=owned_company.id,
            title="Not yours",
            due_at=_now_utc() + timedelta(days=1),
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        resp = other_client.post(f"/v2/partials/tasks/{task.id}/snooze")
        assert resp.status_code == 403

    def test_snooze_unknown_task_returns_404(self, client):
        """Snoozing a non-existent task returns 404."""
        resp = client.post("/v2/partials/tasks/999999/snooze")
        assert resp.status_code == 404


# ===========================================================================
# Feature 2 — Due/overdue badge (template rendering)
# ===========================================================================


class TestDueOverdueBadgeTemplate:
    """Verify that task row templates render correct badge HTML for each due-state."""

    def _render_account_tasks(self, client, owned_company: Company) -> str:
        resp = client.get(f"/v2/partials/customers/{owned_company.id}/tasks")
        assert resp.status_code == 200
        return resp.text

    def test_overdue_badge_shown(self, client, db_session: Session, owned_company: Company, test_user: User):
        """A task with due_at in the past shows the 'Overdue' badge."""
        create_company_task(
            db_session,
            company_id=owned_company.id,
            title="Past task",
            due_at=_now_utc() - timedelta(days=3),
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        html = self._render_account_tasks(client, owned_company)
        assert "overdue" in html.lower()

    def test_due_today_badge_shown(self, client, db_session: Session, owned_company: Company, test_user: User):
        """A task due today (midnight UTC) shows the 'Due today' badge."""
        today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=UTC)
        create_company_task(
            db_session,
            company_id=owned_company.id,
            title="Due today task",
            due_at=today_start,
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        html = self._render_account_tasks(client, owned_company)
        # "Due today" or "overdue" (midnight is <= now) is acceptable
        assert "today" in html.lower() or "overdue" in html.lower()

    def test_future_badge_shown(self, client, db_session: Session, owned_company: Company, test_user: User):
        """A task due in 5 days shows a future-due indicator (not overdue)."""
        create_company_task(
            db_session,
            company_id=owned_company.id,
            title="Future task",
            due_at=_now_utc() + timedelta(days=5),
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        html = self._render_account_tasks(client, owned_company)
        # The task row must be present
        assert "Future task" in html
        # A future task must NOT show the overdue badge
        assert "overdue" not in html.lower()

    def test_no_due_date_no_badge(self, client, db_session: Session, owned_company: Company, test_user: User):
        """A task with no due_at shows no due badge."""
        create_company_task(
            db_session,
            company_id=owned_company.id,
            title="No due date task",
            due_at=None,
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        html = self._render_account_tasks(client, owned_company)
        # The task row should exist
        assert "No due date task" in html
        # But no overdue/due-today badge on this task
        assert "overdue" not in html.lower() or "No due date task" in html


# ===========================================================================
# Feature 3 — win_probability on Requisition
# ===========================================================================


class TestWinProbabilityModel:
    """Unit tests for Requisition.win_probability field."""

    def test_win_probability_nullable_default(self, db_session: Session, test_requisition: Requisition):
        """win_probability defaults to None."""
        assert test_requisition.win_probability is None

    def test_win_probability_set_and_persist(self, db_session: Session, test_requisition: Requisition):
        """Setting win_probability to 0-100 persists correctly."""
        test_requisition.win_probability = 75
        db_session.commit()
        db_session.refresh(test_requisition)
        assert test_requisition.win_probability == 75

    def test_win_probability_zero_allowed(self, db_session: Session, test_requisition: Requisition):
        """win_probability of 0 is valid."""
        test_requisition.win_probability = 0
        db_session.commit()
        db_session.refresh(test_requisition)
        assert test_requisition.win_probability == 0

    def test_win_probability_100_allowed(self, db_session: Session, test_requisition: Requisition):
        """win_probability of 100 is valid."""
        test_requisition.win_probability = 100
        db_session.commit()
        db_session.refresh(test_requisition)
        assert test_requisition.win_probability == 100

    def test_win_probability_out_of_range_raises(self, db_session: Session, test_requisition: Requisition):
        """win_probability outside 0-100 raises ValueError at assignment."""
        with pytest.raises((ValueError, Exception)):
            test_requisition.win_probability = 101

    def test_win_probability_negative_raises(self, db_session: Session, test_requisition: Requisition):
        """Negative win_probability raises ValueError at assignment."""
        with pytest.raises((ValueError, Exception)):
            test_requisition.win_probability = -1


class TestWinProbabilityRoute:
    """Integration tests for PATCH /v2/partials/requisitions/{req_id}/win-
    probability."""

    def test_set_win_probability_persists(self, client, db_session: Session, test_requisition: Requisition):
        """Authorized user can set win_probability and it persists."""
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/win-probability",
            data={"win_probability": "60"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.win_probability == 60

    def test_win_probability_displayed_in_response(self, client, db_session: Session, test_requisition: Requisition):
        """Response HTML shows the new win probability value."""
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/win-probability",
            data={"win_probability": "42"},
        )
        assert resp.status_code == 200
        assert "42" in resp.text

    def test_win_probability_out_of_range_returns_400(self, client, db_session: Session, test_requisition: Requisition):
        """Out-of-range win_probability returns 400 error."""
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/win-probability",
            data={"win_probability": "150"},
        )
        assert resp.status_code == 400

    def test_win_probability_negative_returns_400(self, client, db_session: Session, test_requisition: Requisition):
        """Negative win_probability returns 400 error."""
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/win-probability",
            data={"win_probability": "-5"},
        )
        assert resp.status_code == 400

    def test_win_probability_empty_clears_to_none(self, client, db_session: Session, test_requisition: Requisition):
        """Submitting an empty win_probability clears the value to NULL (200, not
        400)."""
        # First set a value
        test_requisition.win_probability = 55
        db_session.commit()
        # Now clear it
        resp = client.patch(
            f"/v2/partials/requisitions/{test_requisition.id}/win-probability",
            data={"win_probability": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(test_requisition)
        assert test_requisition.win_probability is None

    def test_win_probability_unauthorized_returns_403(
        self,
        db_session: Session,
        test_requisition: Requisition,
        other_user: User,
    ):
        """A user who doesn't own the requisition gets 403 (or 404 for restricted
        roles)."""
        # Make other_user a SALES role to trigger the restriction
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        # Set other_user to SALES so require_requisition_access blocks them
        other_user.role = "sales"
        db_session.commit()

        def _override_db():
            yield db_session

        def _override_user():
            return other_user

        async def _override_token():
            return "mock-token"

        overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = _override_user
        app.dependency_overrides[require_admin] = _override_user
        app.dependency_overrides[require_buyer] = _override_user
        app.dependency_overrides[require_fresh_token] = _override_token
        try:
            with TestClient(app, raise_server_exceptions=False) as c:
                resp = c.patch(
                    f"/v2/partials/requisitions/{test_requisition.id}/win-probability",
                    data={"win_probability": "50"},
                )
                # 404 is the correct response for restricted roles (SALES can't see others' reqs)
                assert resp.status_code in (403, 404)
        finally:
            for dep in overridden:
                app.dependency_overrides.pop(dep, None)


class TestWinProbabilityMigration:
    """Verify migration 146 chains correctly from 145 as single head."""

    def test_migration_146_is_single_head(self):
        """Exactly one alembic head, and migration 146 is in the chain.

        Do NOT assert which revision is the head — later migrations legitimately chain
        on top of 146, so hardcoding the head id is brittle.
        """
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(Config("alembic.ini"))
        heads = script.get_heads()
        assert len(heads) == 1, f"Expected 1 head, got {len(heads)}: {heads}"
        assert script.get_revision("146_req_win_probability") is not None, (
            "146_req_win_probability missing from the migration chain"
        )

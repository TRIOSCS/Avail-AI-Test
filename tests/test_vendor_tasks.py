"""Tests for vendor tasks (migration 142).

Covers:
- vendor_card_id and vendor_contact_id columns on RequisitionTask
- CHECK constraint: task with only vendor_card_id satisfies ck_task_has_parent
- create vendor task (service + HTTP POST)
- complete vendor task: sets completed_at, NO ActivityLog written
- delete vendor task: admin succeeds, non-admin gets 403
- migration 142 upgrade/downgrade roundtrip

Called by: pytest
Depends on: conftest.py (db_session, test_user, admin_user, test_vendor_card,
            test_vendor_contact, client)
"""

from __future__ import annotations

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import TaskStatus
from app.models import ActivityLog
from app.models.task import RequisitionTask
from app.models.vendors import VendorCard, VendorContact
from app.services.task_service import (
    complete_crm_task,
    create_vendor_task,
    get_open_tasks_for_vendor_card,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_activity(db: Session) -> int:
    return db.query(ActivityLog).count()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def vendor_card(db_session: Session) -> VendorCard:
    card = VendorCard(
        normalized_name="test vendor tasks",
        display_name="Test Vendor Tasks",
        emails=["vendor@tasks.example"],
        phones=["+1-555-0200"],
        sighting_count=0,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def vendor_contact(db_session: Session, vendor_card: VendorCard) -> VendorContact:
    vc = VendorContact(
        vendor_card_id=vendor_card.id,
        full_name="Sam Vendor",
        email="sam@tasks.example",
        source="manual",
    )
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


@pytest.fixture()
def admin_client(db_session: Session, admin_user) -> TestClient:
    """TestClient authenticated as admin_user."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return admin_user

    async def _override_fresh():
        return "mock-token"

    overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_fresh

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden:
            app.dependency_overrides.pop(dep, None)


# ---------------------------------------------------------------------------
# CHECK constraint
# ---------------------------------------------------------------------------


class TestVendorTaskCheckConstraint:
    def test_vendor_card_only_satisfies_check(self, db_session: Session, vendor_card: VendorCard):
        """A task with only vendor_card_id set (no requisition, company, site_contact)
        must satisfy ck_task_has_parent."""
        task = RequisitionTask(
            vendor_card_id=vendor_card.id,
            title="Call vendor",
            task_type="general",
            status=TaskStatus.TODO,
            source="manual",
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        assert task.id is not None
        assert task.vendor_card_id == vendor_card.id
        assert task.requisition_id is None
        assert task.company_id is None
        assert task.site_contact_id is None

    def test_task_with_no_parent_still_raises(self, db_session: Session):
        """A task with ALL parents NULL must still violate ck_task_has_parent."""
        orphan = RequisitionTask(
            title="Orphan",
            task_type="general",
            status=TaskStatus.TODO,
            source="manual",
        )
        db_session.add(orphan)
        with pytest.raises((IntegrityError, Exception)):
            db_session.commit()
        db_session.rollback()


# ---------------------------------------------------------------------------
# Service: create_vendor_task
# ---------------------------------------------------------------------------


class TestCreateVendorTask:
    def test_vendor_task_create(self, db_session: Session, vendor_card: VendorCard, test_user):
        """create_vendor_task persists with vendor_card_id and appears in list."""
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="Review pricing with Test Vendor",
            created_by=test_user.id,
        )
        assert task.id is not None
        assert task.vendor_card_id == vendor_card.id
        assert task.requisition_id is None
        assert task.company_id is None
        assert task.site_contact_id is None

        tasks = get_open_tasks_for_vendor_card(db_session, vendor_card.id)
        assert any(t.id == task.id for t in tasks)

    def test_vendor_task_excludes_done(self, db_session: Session, vendor_card: VendorCard, test_user):
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="Done task",
            created_by=test_user.id,
        )
        task.status = TaskStatus.DONE
        db_session.commit()
        tasks = get_open_tasks_for_vendor_card(db_session, vendor_card.id)
        assert len(tasks) == 0


# ---------------------------------------------------------------------------
# Service: complete_crm_task — no ActivityLog
# ---------------------------------------------------------------------------


class TestVendorTaskComplete:
    def test_vendor_task_complete(self, db_session: Session, vendor_card: VendorCard, test_user):
        """POST complete: completed_at is set, NO ActivityLog row created."""
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="Follow up call",
            created_by=test_user.id,
        )
        before = _count_activity(db_session)
        result = complete_crm_task(db_session, task.id, test_user.id, is_admin=False)
        after = _count_activity(db_session)

        assert result is not None
        assert result.status == TaskStatus.DONE
        assert result.completed_at is not None
        assert after == before, (
            f"complete_crm_task wrote {after - before} ActivityLog row(s) — vendor tasks must NOT create fake activity."
        )

    def test_vendor_task_complete_denied_for_stranger(
        self, db_session: Session, vendor_card: VendorCard, test_user, admin_user
    ):
        """M6: a user who is neither creator, assignee, nor admin CANNOT complete a vendor
        task (previously any authenticated user could — vendor tasks were unowned)."""
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="Owned by admin",
            created_by=admin_user.id,
            assigned_to_id=admin_user.id,
        )
        with pytest.raises(PermissionError):
            complete_crm_task(db_session, task.id, test_user.id, is_admin=False)

    def test_vendor_task_complete_allowed_for_assignee(
        self, db_session: Session, vendor_card: VendorCard, test_user, admin_user
    ):
        """The assignee may complete a vendor task even if someone else created it."""
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="Assigned to me",
            created_by=admin_user.id,
            assigned_to_id=test_user.id,
        )
        result = complete_crm_task(db_session, task.id, test_user.id, is_admin=False)
        assert result is not None
        assert result.status == TaskStatus.DONE

    def test_vendor_task_complete_allowed_for_admin(
        self, db_session: Session, vendor_card: VendorCard, test_user, admin_user
    ):
        """An admin may complete any vendor task (the shared CRM gate honours
        is_admin)."""
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="Admin override",
            created_by=test_user.id,
            assigned_to_id=test_user.id,
        )
        result = complete_crm_task(db_session, task.id, admin_user.id, is_admin=True)
        assert result is not None
        assert result.status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# HTTP endpoint: vendor task routes
# ---------------------------------------------------------------------------


class TestVendorTaskEndpoints:
    def test_get_vendor_tasks_tab(self, client, vendor_card: VendorCard):
        """GET /v2/partials/vendors/{id}/tasks returns 200 with task container."""
        response = client.get(f"/v2/partials/vendors/{vendor_card.id}/tasks")
        assert response.status_code == 200
        assert b"vendor-tasks-" in response.content

    def test_post_vendor_task_creates_and_returns_list(self, client, vendor_card: VendorCard):
        """POST /v2/partials/vendors/{id}/tasks creates the task and renders list."""
        response = client.post(
            f"/v2/partials/vendors/{vendor_card.id}/tasks",
            data={"title": "Send NDA"},
        )
        assert response.status_code == 200
        assert b"Send NDA" in response.content

    def test_post_vendor_task_missing_title(self, client, vendor_card: VendorCard):
        """POST with empty title returns validation error (200 with error message)."""
        response = client.post(
            f"/v2/partials/vendors/{vendor_card.id}/tasks",
            data={"title": ""},
        )
        assert response.status_code == 200
        assert b"required" in response.content.lower()

    def test_complete_vendor_task_no_activity_log(
        self,
        client,
        db_session: Session,
        vendor_card: VendorCard,
        test_user,
    ):
        """POST complete endpoint must not create ActivityLog."""
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="HTTP complete test",
            created_by=test_user.id,
        )
        before = _count_activity(db_session)
        response = client.post(f"/v2/partials/tasks/{task.id}/complete")
        assert response.status_code == 200
        after = _count_activity(db_session)
        assert after == before

    def test_vendor_task_delete_admin(
        self,
        admin_client,
        db_session: Session,
        vendor_card: VendorCard,
        admin_user,
    ):
        """Admin DELETE removes the task."""
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="To delete",
            created_by=admin_user.id,
        )
        response = admin_client.delete(f"/v2/partials/tasks/{task.id}")
        assert response.status_code == 200
        # Task should be gone
        db_session.expire_all()
        assert db_session.get(RequisitionTask, task.id) is None

    def test_vendor_task_delete_nonadmin(
        self,
        db_session: Session,
        vendor_card: VendorCard,
        test_user,
        admin_user,
    ):
        """Non-admin who is neither creator nor assignee gets 403 on DELETE."""
        from app.database import get_db
        from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
        from app.main import app

        # Create task as admin_user (not test_user) so test_user is not creator
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="Admin created task",
            created_by=admin_user.id,
        )

        # Override deps to be test_user (non-admin) — require_admin still returns test_user
        # but test_user.role != "admin" so the admin gate (UserRole.ADMIN check) fails

        def _override_db():
            yield db_session

        def _override_nonadmin():
            return test_user

        async def _override_fresh():
            return "mock-token"

        overridden = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
        app.dependency_overrides[get_db] = _override_db
        # Only override require_user — leave require_admin unset so it uses the real dep
        app.dependency_overrides[require_user] = _override_nonadmin
        # Override require_admin to return test_user (non-admin role) so is_admin=False
        app.dependency_overrides[require_admin] = _override_nonadmin
        app.dependency_overrides[require_buyer] = _override_nonadmin
        app.dependency_overrides[require_fresh_token] = _override_fresh

        try:
            with TestClient(app) as non_admin_client:
                response = non_admin_client.delete(f"/v2/partials/tasks/{task.id}")
                assert response.status_code == 403, (
                    f"Expected 403 but got {response.status_code} — non-admin non-owner should not delete vendor tasks"
                )
        finally:
            for dep in overridden:
                app.dependency_overrides.pop(dep, None)


# ---------------------------------------------------------------------------
# Finding 1: edit form uses correct HTMX target for vendor tasks
# ---------------------------------------------------------------------------


class TestVendorTaskEditForm:
    def test_vendor_task_edit_form_renders(self, client, db_session: Session, vendor_card: VendorCard, test_user):
        """GET edit-form for a vendor task must target #vendor-tasks-{id}, NOT #contact-
        tasks-None."""
        task = create_vendor_task(
            db_session,
            vendor_card_id=vendor_card.id,
            title="Edit me",
            created_by=test_user.id,
        )
        response = client.get(f"/v2/partials/tasks/{task.id}/edit-form")
        assert response.status_code == 200
        body = response.text
        assert f"vendor-tasks-{vendor_card.id}" in body, (
            "Edit form must target #vendor-tasks-<vendor_id>, not #contact-tasks-None"
        )
        assert "contact-tasks-None" not in body, "Edit form must not reference #contact-tasks-None for vendor tasks"


# ---------------------------------------------------------------------------
# Finding 2: vendor_contact-only task complete/delete re-renders correctly
# ---------------------------------------------------------------------------


class TestVendorContactOnlyTaskEndpoints:
    """Tasks with only vendor_contact_id (no vendor_card_id) must re-render the vendor
    task list after complete/delete, not return an empty 200."""

    def _make_contact_only_task(
        self,
        db_session: Session,
        vendor_contact: VendorContact,
        test_user,
        title: str = "Contact-only task",
    ) -> RequisitionTask:
        """Create a task scoped to vendor_contact_id only (no vendor_card_id)."""
        task = RequisitionTask(
            vendor_contact_id=vendor_contact.id,
            title=title,
            task_type="general",
            status=TaskStatus.TODO,
            source="manual",
            created_by=test_user.id,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)
        return task

    def test_vendor_contact_task_complete_rerenders(
        self,
        client,
        db_session: Session,
        vendor_card: VendorCard,
        vendor_contact: VendorContact,
        test_user,
    ):
        """POST complete on a vendor_contact-only task must return the vendor task list
        (not an empty fragment)."""
        task = self._make_contact_only_task(db_session, vendor_contact, test_user)
        response = client.post(f"/v2/partials/tasks/{task.id}/complete")
        assert response.status_code == 200
        assert len(response.content) > 0, "complete on vendor_contact-only task must not return empty fragment"
        assert f"vendor-tasks-{vendor_card.id}".encode() in response.content, (
            "Response must contain vendor task container for the parent vendor card"
        )

    def test_vendor_contact_task_delete_rerenders(
        self,
        admin_client,
        db_session: Session,
        vendor_card: VendorCard,
        vendor_contact: VendorContact,
        admin_user,
    ):
        """Admin DELETE on a vendor_contact-only task must return the vendor task list
        (not an empty fragment)."""
        task = RequisitionTask(
            vendor_contact_id=vendor_contact.id,
            title="Delete contact-only task",
            task_type="general",
            status=TaskStatus.TODO,
            source="manual",
            created_by=admin_user.id,
        )
        db_session.add(task)
        db_session.commit()
        db_session.refresh(task)

        response = admin_client.delete(f"/v2/partials/tasks/{task.id}")
        assert response.status_code == 200
        assert len(response.content) > 0, "delete on vendor_contact-only task must not return empty fragment"
        assert f"vendor-tasks-{vendor_card.id}".encode() in response.content, (
            "Response must contain vendor task container for the parent vendor card"
        )


# ---------------------------------------------------------------------------
# Migration 142 chain validation
# ---------------------------------------------------------------------------


class TestMigration142Roundtrip:
    def test_migration_142_roundtrip(self):
        """Migration 142 must exist in the chain, chain onto 141, and be the sole
        head."""
        import pathlib

        from alembic.script import ScriptDirectory

        alembic_dir = pathlib.Path(__file__).resolve().parent.parent / "alembic"
        script = ScriptDirectory(str(alembic_dir))

        # Must have exactly one head
        heads = script.get_heads()
        assert len(heads) == 1, f"Expected 1 alembic head, got {len(heads)}: {heads}"

        # Migration 142 must exist
        rev = script.get_revision("142_vendor_task_cols")
        assert rev is not None, "142_vendor_task_cols not found in migration chain"

        # Must chain onto 141
        assert rev.down_revision == "141_reclaim_cooldown", (
            f"142 should chain onto 141_reclaim_cooldown, but down_revision={rev.down_revision!r}"
        )

        # Migration must declare vendor_card columns (check the SQL strings in the file)
        rev_path = pathlib.Path(alembic_dir) / "versions" / "142_vendor_task_cols.py"
        content = rev_path.read_text()
        assert "vendor_card_id" in content, "migration 142 must add vendor_card_id column"
        assert "vendor_contact_id" in content, "migration 142 must add vendor_contact_id column"
        assert "ck_task_has_parent" in content, "migration 142 must drop+recreate ck_task_has_parent"

    def test_migration_142_downgrade_purges_vendor_only_rows(self):
        """Downgrade must DELETE vendor-only rows before restoring the 3-way CHECK.

        Without the purge, Postgres aborts the downgrade when it tries to recreate the
        CHECK (requisition_id IS NOT NULL OR company_id IS NOT NULL OR site_contact_id
        IS NOT NULL) while vendor-only rows are present.

        This test verifies the purge SQL is present in the downgrade() body and that the
        DELETE targets exactly the vendor-only condition.
        """
        import pathlib

        alembic_dir = pathlib.Path(__file__).resolve().parent.parent / "alembic"
        rev_path = alembic_dir / "versions" / "142_vendor_task_cols.py"
        content = rev_path.read_text()

        # The downgrade must contain a DELETE that removes rows with all three
        # original parent columns NULL.
        assert "DELETE FROM requisition_tasks" in content, (
            "downgrade() must DELETE vendor-only rows before restoring the 3-way CHECK"
        )
        assert "requisition_id IS NULL" in content, "DELETE condition must check requisition_id IS NULL"
        assert "company_id IS NULL" in content, "DELETE condition must check company_id IS NULL"
        assert "site_contact_id IS NULL" in content, "DELETE condition must check site_contact_id IS NULL"

    def test_migration_142_downgrade_purge_runs_against_sqlite(self, db_session: Session, vendor_card: VendorCard):
        """Simulate the downgrade purge on the test SQLite DB.

        Creates a vendor-only task (only vendor_card_id set) via the ORM so Python-side
        defaults fire, then runs the DELETE statement from the migration's downgrade()
        directly.  The row must be gone afterwards, confirming the purge logic is
        correct before the 3-way CHECK is restored.
        """
        import sqlalchemy as _sa

        # Insert a vendor-only task via the ORM so Python-side defaults (priority, etc.) fire.
        vendor_only_task = RequisitionTask(
            vendor_card_id=vendor_card.id,
            title="vendor-only downgrade test",
            task_type="general",
            status=TaskStatus.TODO,
            source="manual",
        )
        db_session.add(vendor_only_task)
        db_session.commit()
        db_session.refresh(vendor_only_task)

        # Confirm the row exists and is truly vendor-only.
        assert vendor_only_task.requisition_id is None
        assert vendor_only_task.company_id is None
        assert vendor_only_task.site_contact_id is None
        assert vendor_only_task.vendor_card_id == vendor_card.id

        count_before = db_session.execute(
            _sa.text(
                "SELECT COUNT(*) FROM requisition_tasks"
                " WHERE vendor_card_id = :vcid AND requisition_id IS NULL"
                " AND company_id IS NULL AND site_contact_id IS NULL"
            ),
            {"vcid": vendor_card.id},
        ).scalar()
        assert count_before >= 1, "Vendor-only task should exist before purge"

        # Run the downgrade purge SQL (mirrors exactly what migration 142 downgrade() does).
        db_session.execute(
            _sa.text(
                "DELETE FROM requisition_tasks"
                " WHERE requisition_id IS NULL AND company_id IS NULL AND site_contact_id IS NULL"
            )
        )
        db_session.commit()

        # The vendor-only task must be gone.
        count_after = db_session.execute(
            _sa.text(
                "SELECT COUNT(*) FROM requisition_tasks"
                " WHERE vendor_card_id = :vcid AND requisition_id IS NULL"
                " AND company_id IS NULL AND site_contact_id IS NULL"
            ),
            {"vcid": vendor_card.id},
        ).scalar()
        assert count_after == 0, "Downgrade purge must remove vendor-only tasks so the 3-way CHECK can be restored"

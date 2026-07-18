"""test_workspace_notes_attachments.py — notes threads + file attachments
(Approvals Workspace 2.4, designs D2 + D3).

Covers workspace_notes (add_note / notes_thread / note_counts — narrowest-subject
scoping, decision tags, blank-body refusal), the notes route (never status-locked,
exactly-one-subject, access via the owning plan), the attachments routes (multipart
→ store_and_attach with BuyPlanAttachment + the right fk_field, validate_subject,
ATTACH_ADDED / ATTACH_REMOVED activity, uploader-or-manager delete), and the thread
rendering embedded in all three panes. Graph storage is mocked at the source module
(app.services.attachment_service) like the existing attachment tests.

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs builders,
            app.services.workspace_notes, app.routers.htmx.approvals_hub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ActivityType, BuyPlanLineStatus, BuyPlanStatus, UserRole
from app.database import get_db
from app.dependencies import require_buyplan_approver, require_buyplan_po_approver, require_user
from app.models import ActivityLog, User
from app.models.buy_plan import BuyPlanAttachment
from app.services.workspace_notes import add_note, note_counts, notes_thread
from tests.test_approvals_hub_tabs import _line, _plan, _req_quote


@pytest.fixture()
def hub_client(db_session: Session, test_user: User):
    from app.main import app

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    app.dependency_overrides[require_buyplan_po_approver] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_buyplan_approver, require_buyplan_po_approver):
            app.dependency_overrides.pop(dep, None)


def _plan_and_line(db: Session, user: User, *, status: str = BuyPlanStatus.ACTIVE.value):
    req, q, rq = _req_quote(db, user)
    bp = _plan(db, req, q, status=status)
    line = _line(db, bp, rq, user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db.commit()
    return bp, line


def _fake_store(db: Session):
    """A store_and_attach stand-in that skips Graph but persists a real row (the
    same shape the real service returns)."""

    async def _store(db_arg, *, model, fk_field, entity_label, entity_id, file, user):
        att = model(
            **{fk_field: entity_id},
            file_name=file.filename or "file.pdf",
            library_item_id=None,  # OneDrive-less row — remove_attachment skips cloud
            library_web_url=f"https://lib.example.com/{file.filename}",
            content_type=file.content_type,
            size_bytes=3,
            uploaded_by_id=user.id,
            created_at=datetime.now(UTC),
        )
        db_arg.add(att)
        db_arg.commit()
        db_arg.refresh(att)
        return att

    return _store


# ── Service: add_note / notes_thread / note_counts ───────────────────────


class TestWorkspaceNotesService:
    def test_add_note_blank_body_refused(self, db_session, test_user):
        bp, _line_ = _plan_and_line(db_session, test_user)
        with pytest.raises(ValueError):
            add_note(db_session, user=test_user, body="   ", buy_plan_id=bp.id)

    def test_add_note_unknown_decision_refused(self, db_session, test_user):
        bp, _line_ = _plan_and_line(db_session, test_user)
        with pytest.raises(ValueError):
            add_note(db_session, user=test_user, body="x", buy_plan_id=bp.id, decision="maybe")

    def test_thread_scoping_is_narrowest_subject(self, db_session, test_user):
        bp, line = _plan_and_line(db_session, test_user)
        add_note(db_session, user=test_user, body="plan-level note", buy_plan_id=bp.id)
        add_note(db_session, user=test_user, body="line note", buy_plan_id=bp.id, buy_plan_line_id=line.id)
        db_session.commit()

        plan_thread = notes_thread(db_session, buy_plan_id=bp.id)
        assert [n.notes for n in plan_thread] == ["plan-level note"]  # line note excluded
        line_thread = notes_thread(db_session, buy_plan_line_id=line.id)
        assert [n.notes for n in line_thread] == ["line note"]

    def test_thread_requires_exactly_one_subject(self, db_session):
        with pytest.raises(ValueError):
            notes_thread(db_session)
        with pytest.raises(ValueError):
            notes_thread(db_session, buy_plan_id=1, buy_plan_line_id=2)

    def test_note_counts_batched(self, db_session, test_user):
        bp, line = _plan_and_line(db_session, test_user)
        add_note(db_session, user=test_user, body="a", buy_plan_id=bp.id, buy_plan_line_id=line.id)
        add_note(db_session, user=test_user, body="b", buy_plan_id=bp.id, buy_plan_line_id=line.id)
        add_note(db_session, user=test_user, body="c", buy_plan_id=bp.id)
        db_session.commit()

        assert note_counts(db_session, buy_plan_line_ids=[line.id]) == {line.id: 2}
        assert note_counts(db_session, buy_plan_ids=[bp.id]) == {bp.id: 1}  # plan-level only
        assert note_counts(db_session, prepayment_ids=[]) == {}


# ── Notes route ──────────────────────────────────────────────────────────


class TestNotesRoute:
    def test_add_plan_note_renders_thread(self, hub_client, db_session, test_user):
        bp, _line_ = _plan_and_line(db_session, test_user)

        r = hub_client.post(
            "/v2/partials/approvals/notes", data={"buy_plan_id": bp.id, "body": "customer wants ESD trays"}
        )
        assert r.status_code == 200
        assert "customer wants ESD trays" in r.text
        assert 'id="aw-notes-thread"' in r.text
        row = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.NOTE.value).one()
        assert row.buy_plan_id == bp.id
        assert row.buy_plan_line_id is None

    def test_add_line_note_lands_on_line(self, hub_client, db_session, test_user):
        _bp, line = _plan_and_line(db_session, test_user)

        r = hub_client.post(
            "/v2/partials/approvals/notes", data={"buy_plan_line_id": line.id, "body": "vendor confirmed stock"}
        )
        assert r.status_code == 200
        row = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.NOTE.value).one()
        assert row.buy_plan_line_id == line.id
        assert row.buy_plan_id == line.buy_plan_id

    def test_notes_never_status_locked(self, hub_client, db_session, test_user):
        """A COMPLETED (field-locked) plan still takes notes — spec §7."""
        bp, _line_ = _plan_and_line(db_session, test_user, status=BuyPlanStatus.COMPLETED.value)

        r = hub_client.post("/v2/partials/approvals/notes", data={"buy_plan_id": bp.id, "body": "post-mortem"})
        assert r.status_code == 200
        assert db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.NOTE.value).count() == 1

    def test_blank_body_400s(self, hub_client, db_session, test_user):
        bp, _line_ = _plan_and_line(db_session, test_user)
        r = hub_client.post("/v2/partials/approvals/notes", data={"buy_plan_id": bp.id, "body": "  "})
        assert r.status_code == 400

    def test_two_subjects_400(self, hub_client, db_session, test_user):
        bp, line = _plan_and_line(db_session, test_user)
        r = hub_client.post(
            "/v2/partials/approvals/notes",
            data={"buy_plan_id": bp.id, "buy_plan_line_id": line.id, "body": "x"},
        )
        assert r.status_code == 400

    def test_thread_renders_on_all_three_panes(self, hub_client, db_session, test_user):
        from decimal import Decimal

        from app.models.quality_plan import Prepayment

        bp, line = _plan_and_line(db_session, test_user)
        pp = Prepayment(
            buy_plan_id=bp.id,
            buy_plan_line_id=line.id,
            total_incl_fees=Decimal("100.00"),
            currency="USD",
            payment_method="wire",
            vendor_name="Acme Dist",
            created_by_id=test_user.id,
        )
        db_session.add(pp)
        db_session.commit()

        for url in (
            f"/v2/partials/approvals/plan/{bp.id}/pane",
            f"/v2/partials/approvals/po/{line.id}/pane",
            f"/v2/partials/approvals/prepayments/{pp.id}/pane",
        ):
            body = hub_client.get(url).text
            assert 'id="aw-notes-thread"' in body
            assert "/v2/partials/approvals/notes" in body

    def test_decision_tagged_note_styled_distinctly(self, hub_client, db_session, test_user):
        bp, _line_ = _plan_and_line(db_session, test_user)
        add_note(db_session, user=test_user, body="fix the sell", buy_plan_id=bp.id, decision="sent_back")
        db_session.commit()

        body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
        assert "Sent back" in body
        assert "fix the sell" in body


# ── Attachments routes ───────────────────────────────────────────────────


class TestAttachmentsRoutes:
    def _upload(self, client, db, data):
        with patch(
            "app.services.attachment_service.store_and_attach",
            side_effect=_fake_store(db),
        ):
            return client.post(
                "/v2/partials/approvals/attachments",
                data=data,
                files={"file": ("coc.pdf", b"pdf", "application/pdf")},
            )

    def test_upload_to_line_sets_fk_and_logs(self, hub_client, db_session, test_user):
        _bp, line = _plan_and_line(db_session, test_user)

        r = self._upload(hub_client, db_session, {"buy_plan_line_id": str(line.id)})
        assert r.status_code == 200
        assert "coc.pdf" in r.text
        assert "https://lib.example.com/coc.pdf" in r.text  # library_web_url link
        att = db_session.query(BuyPlanAttachment).one()
        assert att.buy_plan_line_id == line.id
        assert att.buy_plan_id is None and att.prepayment_id is None
        att.validate_subject()  # exactly one set
        log = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.ATTACH_ADDED.value).one()
        assert log.buy_plan_line_id == line.id
        assert "coc.pdf" in log.summary

    def test_upload_to_plan_and_prepayment_fk(self, hub_client, db_session, test_user):
        from decimal import Decimal

        from app.models.quality_plan import Prepayment

        bp, line = _plan_and_line(db_session, test_user)
        pp = Prepayment(
            buy_plan_id=bp.id,
            buy_plan_line_id=line.id,
            total_incl_fees=Decimal("100.00"),
            currency="USD",
            payment_method="wire",
            vendor_name="Acme Dist",
            created_by_id=test_user.id,
        )
        db_session.add(pp)
        db_session.commit()

        assert self._upload(hub_client, db_session, {"buy_plan_id": str(bp.id)}).status_code == 200
        assert self._upload(hub_client, db_session, {"prepayment_id": str(pp.id)}).status_code == 200
        by_fk = {
            (a.buy_plan_id, a.buy_plan_line_id, a.prepayment_id) for a in db_session.query(BuyPlanAttachment).all()
        }
        assert (bp.id, None, None) in by_fk
        assert (None, None, pp.id) in by_fk

    def test_upload_without_subject_400(self, hub_client, db_session, test_user):
        _plan_and_line(db_session, test_user)
        r = self._upload(hub_client, db_session, {})
        assert r.status_code == 400

    def test_delete_by_uploader_logs_removed(self, hub_client, db_session, test_user):
        bp, _line_ = _plan_and_line(db_session, test_user)
        att = BuyPlanAttachment(buy_plan_id=bp.id, file_name="old.pdf", uploaded_by_id=test_user.id)
        db_session.add(att)
        db_session.commit()

        r = hub_client.delete(f"/v2/partials/approvals/attachments/{att.id}")
        assert r.status_code == 200
        assert db_session.query(BuyPlanAttachment).count() == 0
        log = db_session.query(ActivityLog).filter(ActivityLog.activity_type == ActivityType.ATTACH_REMOVED.value).one()
        assert "old.pdf" in log.summary

    def test_delete_by_stranger_403s(self, hub_client, db_session, test_user):
        import uuid

        bp, _line_ = _plan_and_line(db_session, test_user)
        other = User(
            email=f"up-{uuid.uuid4().hex[:6]}@t.com",
            name="Uploader",
            role="buyer",
            azure_id=f"az-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(other)
        db_session.flush()
        att = BuyPlanAttachment(buy_plan_id=bp.id, file_name="theirs.pdf", uploaded_by_id=other.id)
        db_session.add(att)
        db_session.commit()

        r = hub_client.delete(f"/v2/partials/approvals/attachments/{att.id}")
        assert r.status_code == 403
        assert db_session.query(BuyPlanAttachment).count() == 1

    def test_delete_by_manager_allowed(self, hub_client, db_session, test_user):
        import uuid

        bp, _line_ = _plan_and_line(db_session, test_user)
        other = User(
            email=f"up-{uuid.uuid4().hex[:6]}@t.com",
            name="Uploader",
            role="buyer",
            azure_id=f"az-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(other)
        db_session.flush()
        att = BuyPlanAttachment(buy_plan_id=bp.id, file_name="theirs.pdf", uploaded_by_id=other.id)
        db_session.add(att)
        test_user.role = UserRole.MANAGER.value
        db_session.commit()

        r = hub_client.delete(f"/v2/partials/approvals/attachments/{att.id}")
        assert r.status_code == 200
        assert db_session.query(BuyPlanAttachment).count() == 0

    def test_delete_missing_404s(self, hub_client):
        assert hub_client.delete("/v2/partials/approvals/attachments/999999").status_code == 404

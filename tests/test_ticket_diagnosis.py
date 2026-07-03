"""test_ticket_diagnosis.py — AI diagnosis service + admin endpoints + gating.

Covers the "Diagnose + fix-prompt" feature: the ticket_diagnosis_service persists a
structured diagnosis + generated fix prompt, the admin diagnose/diagnose-bulk/bulk-status
endpoints behave, the console view + screenshot routes are admin-only, and the public
report-submit path stays open to any authenticated user.

Called by: pytest
Depends on: conftest.py fixtures (db_session, test_user, admin_user)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_admin, require_user
from app.main import app
from app.models import User
from app.models.trouble_ticket import TroubleTicket
from app.utils.claude_errors import ClaudeError, ClaudeUnavailableError

_PATCH_TARGET = "app.services.ticket_diagnosis_service.claude_structured_with_usage"

_FAKE_RESULT = {
    "root_cause": "Submit handler swallows the validation error",
    "severity": "high",
    "affected_areas": ["app/routers/error_reports.py", "submit handler"],
    "reproduction_steps": ["Open the form", "Submit empty", "Observe silent failure"],
    "fix_prompt": "In app/routers/error_reports.py, surface the 422 to the user...",
}
_FAKE_USAGE = {"input_tokens": 1200, "output_tokens": 300}


# ── Client builders (explicit auth control — conftest's `client` bypasses admin) ──


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _admin_client(db_session: Session, user: User) -> TestClient:
    """Client where require_admin succeeds as *user* (admin happy path)."""
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    return TestClient(app)


def _nonadmin_client(db_session: Session, user: User) -> TestClient:
    """Client where require_user is *user* but require_admin denies (403)."""

    def _deny():
        raise HTTPException(403, "Admin access required")

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = _deny
    return TestClient(app)


def _make_ticket(db: Session, *, num: str = "TT-0001", **kw) -> TroubleTicket:
    t = TroubleTicket(
        ticket_number=num,
        title=kw.pop("title", "Something broke"),
        description=kw.pop("description", "The submit button did nothing"),
        status=kw.pop("status", "submitted"),
        source=kw.pop("source", "report_button"),
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── diagnose_ticket service / endpoint ────────────────────────────────


class TestDiagnoseSingle:
    @patch(_PATCH_TARGET, new_callable=AsyncMock)
    def test_diagnose_persists_and_renders(self, mock_ai, db_session, admin_user):
        mock_ai.return_value = (_FAKE_RESULT, _FAKE_USAGE)
        t = _make_ticket(db_session)
        c = _admin_client(db_session, admin_user)

        r = c.post(f"/api/trouble-tickets/{t.id}/diagnose")
        assert r.status_code == 200
        assert "Submit handler swallows" in r.text
        # The fix prompt now rides along as an OOB swap of the shared #ticket-prompt box.
        assert "Copy prompt" in r.text
        assert 'id="ticket-prompt"' in r.text and 'hx-swap-oob="true"' in r.text
        assert r.headers.get("HX-Trigger") == "ticketsUpdated"

        db_session.refresh(t)
        assert t.diagnosis["root_cause"].startswith("Submit handler")
        assert t.generated_prompt.startswith("In app/routers/error_reports.py")
        assert t.diagnosed_at is not None
        assert t.cost_tokens == 1500
        assert t.cost_usd and t.cost_usd > 0

    @patch(_PATCH_TARGET, new_callable=AsyncMock)
    def test_diagnose_ai_unavailable_is_friendly(self, mock_ai, db_session, admin_user):
        mock_ai.side_effect = ClaudeUnavailableError("no key")
        t = _make_ticket(db_session)
        c = _admin_client(db_session, admin_user)

        r = c.post(f"/api/trouble-tickets/{t.id}/diagnose")
        assert r.status_code == 200  # inline amber message, not a 500
        assert "unavailable" in r.text.lower()
        db_session.refresh(t)
        assert t.diagnosis is None

    def test_diagnose_404(self, db_session, admin_user):
        c = _admin_client(db_session, admin_user)
        r = c.post("/api/trouble-tickets/99999/diagnose")
        assert r.status_code == 404

    @patch(_PATCH_TARGET, new_callable=AsyncMock)
    def test_diagnose_truncates_huge_console(self, mock_ai, db_session, admin_user):
        mock_ai.return_value = (_FAKE_RESULT, _FAKE_USAGE)
        t = _make_ticket(db_session)
        t.console_errors = "x" * 50000
        db_session.commit()
        c = _admin_client(db_session, admin_user)

        c.post(f"/api/trouble-tickets/{t.id}/diagnose")
        prompt = mock_ai.call_args.args[0]
        assert len(prompt) < 12000  # console truncated to ~4k, not 50k


# ── bulk diagnose ─────────────────────────────────────────────────────


class TestDiagnoseBulk:
    @patch(_PATCH_TARGET, new_callable=AsyncMock)
    def test_bulk_diagnose_all(self, mock_ai, db_session, admin_user):
        mock_ai.return_value = (_FAKE_RESULT, _FAKE_USAGE)
        ids = [_make_ticket(db_session, num=f"TT-000{i}").id for i in range(1, 4)]
        c = _admin_client(db_session, admin_user)

        r = c.post("/api/trouble-tickets/diagnose-bulk", json={"ticket_ids": ids})
        assert r.status_code == 200
        assert "Diagnosed 3 of 3" in r.text
        assert r.headers.get("HX-Trigger") == "ticketsUpdated"
        for tid in ids:
            assert db_session.get(TroubleTicket, tid).generated_prompt is not None

    @patch(_PATCH_TARGET, new_callable=AsyncMock)
    def test_bulk_diagnose_isolates_failures(self, mock_ai, db_session, admin_user):
        async def _fake(prompt, schema, **kw):
            if "TT-0002" in prompt:
                raise ClaudeError("boom")
            return (_FAKE_RESULT, _FAKE_USAGE)

        mock_ai.side_effect = _fake
        ids = [_make_ticket(db_session, num=f"TT-000{i}").id for i in range(1, 4)]
        c = _admin_client(db_session, admin_user)

        r = c.post("/api/trouble-tickets/diagnose-bulk", json={"ticket_ids": ids})
        assert r.status_code == 200
        assert "Diagnosed 2 of 3" in r.text  # the failing one is isolated


# ── bulk status ───────────────────────────────────────────────────────


class TestBulkStatus:
    def test_bulk_resolve_stamps_resolver(self, db_session, admin_user):
        ids = [_make_ticket(db_session, num=f"TT-000{i}").id for i in range(1, 3)]
        c = _admin_client(db_session, admin_user)

        r = c.post("/api/trouble-tickets/bulk-status", json={"ticket_ids": ids, "status": "resolved"})
        assert r.status_code == 200
        assert r.headers.get("HX-Trigger") == "ticketsUpdated"
        for tid in ids:
            t = db_session.get(TroubleTicket, tid)
            assert t.status == "resolved"
            assert t.resolved_at is not None
            assert t.resolved_by_id == admin_user.id

    def test_bulk_wont_fix_no_resolver(self, db_session, admin_user):
        tid = _make_ticket(db_session).id
        c = _admin_client(db_session, admin_user)
        r = c.post("/api/trouble-tickets/bulk-status", json={"ticket_ids": [tid], "status": "wont_fix"})
        assert r.status_code == 200
        t = db_session.get(TroubleTicket, tid)
        assert t.status == "wont_fix"
        assert t.resolved_at is None

    def test_bulk_status_rejects_invalid(self, db_session, admin_user):
        tid = _make_ticket(db_session).id
        c = _admin_client(db_session, admin_user)
        r = c.post("/api/trouble-tickets/bulk-status", json={"ticket_ids": [tid], "status": "banana"})
        assert r.status_code == 422


# ── admin gating ──────────────────────────────────────────────────────


class TestAdminGating:
    def test_nonadmin_blocked_on_all_admin_routes(self, db_session, test_user):
        t = _make_ticket(db_session)
        c = _nonadmin_client(db_session, test_user)
        assert c.post(f"/api/trouble-tickets/{t.id}/diagnose").status_code == 403
        assert c.post("/api/trouble-tickets/diagnose-bulk", json={"ticket_ids": [t.id]}).status_code == 403
        assert (
            c.post("/api/trouble-tickets/bulk-status", json={"ticket_ids": [t.id], "status": "resolved"}).status_code
            == 403
        )
        assert c.get("/v2/partials/trouble-tickets/workspace").status_code == 403
        assert c.get("/v2/partials/trouble-tickets/list").status_code == 403
        assert c.get(f"/v2/partials/trouble-tickets/{t.id}").status_code == 403
        assert c.get(f"/api/trouble-tickets/{t.id}/screenshot").status_code == 403

    def test_admin_allowed_on_console(self, db_session, admin_user):
        _make_ticket(db_session)
        c = _admin_client(db_session, admin_user)
        assert c.get("/v2/partials/trouble-tickets/workspace").status_code == 200
        assert c.get("/v2/partials/trouble-tickets/list").status_code == 200

    def test_fullpage_console_admin_only(self, db_session, test_user, admin_user):
        """The full-page console (/v2/trouble-tickets) 403s a non-admin, 200s an admin.

        v2_page resolves the user via get_user (session), so patch that like the deep
        tests.
        """
        c = _admin_client(db_session, admin_user)
        with patch("app.routers.htmx_views.get_user", return_value=test_user):
            assert c.get("/v2/trouble-tickets").status_code == 403
        with patch("app.routers.htmx_views.get_user", return_value=admin_user):
            assert c.get("/v2/trouble-tickets").status_code == 200


# ── report submit stays open to any user ──────────────────────────────


class TestSubmitStaysPublic:
    @patch("app.routers.error_reports._generate_ai_summary", new_callable=AsyncMock)
    def test_buyer_can_submit_and_context_persists(self, _mock_summary, db_session, test_user):
        c = _nonadmin_client(db_session, test_user)
        r = c.post(
            "/api/trouble-tickets/submit",
            json={
                "description": "Search returns nothing",
                "page_url": "https://app/v2/search",
                "auto_captured_context": '{"current_view": "search", "app_build": "abc123"}',
            },
        )
        assert r.status_code == 200
        assert "Report submitted" in r.text
        ticket = db_session.query(TroubleTicket).order_by(TroubleTicket.id.desc()).first()
        assert ticket.current_view == "search"
        assert ticket.auto_captured_context["app_build"] == "abc123"

"""test_ticket_kind_and_prompt.py — unified bug+feature ticket kind + Create-Prompt.

Covers the feature-request extension of the trouble-ticket system:
  - the TicketType StrEnum + the trouble_tickets.ticket_type column (default 'bug',
    validated),
  - report submit storing ticket_type='feature' (and defaulting to bug),
  - the inbox kind filter (bug / feature / all) + the composing status+kind pills,
  - the notes-aware, kind-aware Create-Prompt endpoint (Anthropic call mocked).

Called by: pytest
Depends on: conftest.py fixtures (db_session, test_user, admin_user)
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.constants import TicketType
from app.database import get_db
from app.dependencies import require_admin, require_user
from app.main import app
from app.models.trouble_ticket import TroubleTicket

_PROMPT_PATCH = "app.services.ticket_prompt_service.claude_text"


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _admin_client(db_session, user) -> TestClient:
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    return TestClient(app)


def _user_client(db_session, user) -> TestClient:
    """require_user succeeds as *user*; require_admin denies (report submit is
    public)."""

    def _deny():
        raise HTTPException(403, "Admin access required")

    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_admin] = _deny
    return TestClient(app)


def _make_ticket(db, *, num="TT-0001", **kw) -> TroubleTicket:
    t = TroubleTicket(
        ticket_number=num,
        title=kw.pop("title", "Something"),
        description=kw.pop("description", "A description"),
        status=kw.pop("status", "submitted"),
        source=kw.pop("source", "report_button"),
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── column + StrEnum ──────────────────────────────────────────────────


class TestTicketTypeColumn:
    def test_enum_values(self):
        assert TicketType.BUG == "bug"
        assert TicketType.FEATURE == "feature"
        assert {e.value for e in TicketType} == {"bug", "feature"}

    def test_column_defaults_to_bug(self, db_session):
        t = _make_ticket(db_session)
        assert t.ticket_type == TicketType.BUG

    def test_feature_persists(self, db_session):
        t = _make_ticket(db_session, num="TT-0002", ticket_type=TicketType.FEATURE)
        assert t.ticket_type == "feature"

    def test_validator_rejects_bad_value(self, db_session):
        with pytest.raises(ValueError):
            _make_ticket(db_session, num="TT-0003", ticket_type="banana")


# ── submit path ───────────────────────────────────────────────────────


class TestSubmitKind:
    @patch("app.routers.error_reports._generate_ai_summary", new_callable=AsyncMock)
    def test_submit_feature(self, _mock_summary, db_session, test_user):
        c = _user_client(db_session, test_user)
        r = c.post(
            "/api/trouble-tickets/submit",
            json={"description": "Add a dark mode toggle", "ticket_type": "feature"},
        )
        assert r.status_code == 200
        assert "Feature request submitted" in r.text
        t = db_session.query(TroubleTicket).order_by(TroubleTicket.id.desc()).first()
        assert t.ticket_type == TicketType.FEATURE

    @patch("app.routers.error_reports._generate_ai_summary", new_callable=AsyncMock)
    def test_submit_defaults_to_bug(self, _mock_summary, db_session, test_user):
        c = _user_client(db_session, test_user)
        r = c.post("/api/trouble-tickets/submit", json={"description": "It broke"})
        assert r.status_code == 200
        assert "Report submitted" in r.text
        t = db_session.query(TroubleTicket).order_by(TroubleTicket.id.desc()).first()
        assert t.ticket_type == TicketType.BUG

    def test_form_renders_feature_copy(self, db_session, test_user):
        c = _user_client(db_session, test_user)
        r = c.get("/api/trouble-tickets/form?type=feature")
        assert r.status_code == 200
        assert "Request a Feature" in r.text
        r2 = c.get("/api/trouble-tickets/form")
        assert "Report a Problem" in r2.text


# ── inbox kind filter ─────────────────────────────────────────────────


class TestInboxKindFilter:
    def test_filter_by_kind(self, db_session, admin_user):
        _make_ticket(db_session, num="TT-0001", title="Bug one", ticket_type=TicketType.BUG)
        _make_ticket(db_session, num="TT-0002", title="Feature one", ticket_type=TicketType.FEATURE)
        c = _admin_client(db_session, admin_user)

        only_features = c.get("/v2/partials/trouble-tickets/list?type=feature")
        assert only_features.status_code == 200
        assert "Feature one" in only_features.text
        assert "Bug one" not in only_features.text

        only_bugs = c.get("/v2/partials/trouble-tickets/list?type=bug")
        assert "Bug one" in only_bugs.text
        assert "Feature one" not in only_bugs.text

        both = c.get("/v2/partials/trouble-tickets/list")
        assert "Bug one" in both.text and "Feature one" in both.text

    def test_status_and_kind_compose(self, db_session, admin_user):
        _make_ticket(
            db_session, num="TT-0001", title="Open feature", ticket_type=TicketType.FEATURE, status="submitted"
        )
        _make_ticket(db_session, num="TT-0002", title="Done feature", ticket_type=TicketType.FEATURE, status="resolved")
        _make_ticket(db_session, num="TT-0003", title="Open bug", ticket_type=TicketType.BUG, status="submitted")
        c = _admin_client(db_session, admin_user)

        r = c.get("/v2/partials/trouble-tickets/list?status=open&type=feature")
        assert "Open feature" in r.text
        assert "Done feature" not in r.text  # excluded by status
        assert "Open bug" not in r.text  # excluded by kind
        # Pills carry the sibling dimension so the filters compose.
        assert "status=open&type=feature" in r.text or "status=open&amp;type=feature" in r.text


# ── Create-Prompt endpoint ────────────────────────────────────────────


class TestCreatePrompt:
    @patch(_PROMPT_PATCH, new_callable=AsyncMock)
    def test_bug_prompt_generated_and_stored(self, mock_ai, db_session, admin_user):
        mock_ai.return_value = "Fix the submit handler in app/routers/error_reports.py..."
        t = _make_ticket(db_session, ticket_type=TicketType.BUG, current_page="https://app/v2/search")
        c = _admin_client(db_session, admin_user)

        r = c.post(f"/api/trouble-tickets/{t.id}/generate-prompt", data={"admin_notes": "Check the 422 path"})
        assert r.status_code == 200
        assert "Copy prompt" in r.text
        assert "Fix the submit handler" in r.text
        assert r.headers.get("HX-Trigger") == "ticketsUpdated"

        db_session.refresh(t)
        assert t.generated_prompt.startswith("Fix the submit handler")
        assert t.admin_notes == "Check the 422 path"
        # Bug framing + notes reached the model.
        system = mock_ai.call_args.kwargs["system"]
        user_prompt = mock_ai.call_args.args[0]
        assert "fix" in system.lower()
        assert "Check the 422 path" in user_prompt

    @patch(_PROMPT_PATCH, new_callable=AsyncMock)
    def test_feature_prompt_is_build_framed(self, mock_ai, db_session, admin_user):
        mock_ai.return_value = "Brainstorm, then plan, then build the dark mode toggle."
        t = _make_ticket(db_session, ticket_type=TicketType.FEATURE, description="Add dark mode")
        c = _admin_client(db_session, admin_user)

        r = c.post(f"/api/trouble-tickets/{t.id}/generate-prompt", data={"admin_notes": ""})
        assert r.status_code == 200
        db_session.refresh(t)
        assert t.generated_prompt.startswith("Brainstorm")
        system = mock_ai.call_args.kwargs["system"]
        assert "build" in system.lower() and "brainstorm" in system.lower()

    @patch(_PROMPT_PATCH, new_callable=AsyncMock)
    def test_regenerate_incorporates_updated_notes(self, mock_ai, db_session, admin_user):
        mock_ai.return_value = "prompt v2"
        t = _make_ticket(db_session, ticket_type=TicketType.BUG, admin_notes="old note")
        c = _admin_client(db_session, admin_user)

        c.post(f"/api/trouble-tickets/{t.id}/generate-prompt", data={"admin_notes": "brand new note"})
        db_session.refresh(t)
        assert t.admin_notes == "brand new note"
        assert "brand new note" in mock_ai.call_args.args[0]

    def test_generate_prompt_admin_only(self, db_session, test_user):
        t = _make_ticket(db_session)
        c = _user_client(db_session, test_user)
        assert c.post(f"/api/trouble-tickets/{t.id}/generate-prompt").status_code == 403

    def test_generate_prompt_404(self, db_session, admin_user):
        c = _admin_client(db_session, admin_user)
        assert c.post("/api/trouble-tickets/99999/generate-prompt").status_code == 404

    @patch(_PROMPT_PATCH, new_callable=AsyncMock)
    def test_generate_prompt_ai_unavailable_is_friendly(self, mock_ai, db_session, admin_user):
        from app.utils.claude_errors import ClaudeUnavailableError

        mock_ai.side_effect = ClaudeUnavailableError("no key")
        t = _make_ticket(db_session)
        c = _admin_client(db_session, admin_user)
        r = c.post(f"/api/trouble-tickets/{t.id}/generate-prompt")
        assert r.status_code == 200  # inline amber message, not a 500
        assert "unavailable" in r.text.lower()


# ── detail view renders for both kinds ────────────────────────────────


class TestDetailRenders:
    def test_bug_detail_has_create_prompt_and_diagnose(self, db_session, admin_user):
        t = _make_ticket(db_session, ticket_type=TicketType.BUG, admin_notes="a note")
        c = _admin_client(db_session, admin_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "Create Prompt" in r.text
        assert 'id="ticket-prompt"' in r.text
        assert "a note" in r.text  # notes textarea prefilled
        assert "Diagnose with AI" in r.text  # bug shows diagnose
        assert ">Bug<" in r.text  # kind badge

    def test_feature_detail_hides_diagnose(self, db_session, admin_user):
        t = _make_ticket(db_session, num="TT-0009", ticket_type=TicketType.FEATURE)
        c = _admin_client(db_session, admin_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "Create Prompt" in r.text
        assert "Diagnose with AI" not in r.text  # feature has no diagnose section
        assert ">Feature<" in r.text


# ── admin_notes persistence via PATCH ─────────────────────────────────


class TestAdminNotesPatch:
    def test_patch_saves_admin_notes(self, db_session, admin_user):
        t = _make_ticket(db_session)
        c = _admin_client(db_session, admin_user)
        r = c.patch(f"/api/trouble-tickets/{t.id}", json={"admin_notes": "reviewer note"})
        assert r.status_code == 200
        db_session.refresh(t)
        assert t.admin_notes == "reviewer note"

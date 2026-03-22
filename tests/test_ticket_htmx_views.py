"""test_ticket_htmx_views.py — HTMX view tests for Trouble Ticket management UI.

Tests the workspace, list, and detail partials for the management views
introduced in Tasks 5 and 6 of the Trouble Ticket Redesign.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_user
from app.main import app
from app.models import User
from app.models.root_cause_group import RootCauseGroup
from app.models.trouble_ticket import TroubleTicket

# ── Helpers ───────────────────────────────────────────────────────────


def _make_client(db_session: Session, user: User) -> TestClient:
    """Build a TestClient authenticated as the given user."""

    def _override_db():
        yield db_session

    def _override_user():
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    return TestClient(app)


def _make_ticket(
    db: Session,
    *,
    status: str = "submitted",
    title: str = "Test ticket",
    description: str = "Something broke",
    source: str = "report_button",
    ticket_number: str = "TT-0001",
    ai_summary: str | None = None,
    root_cause_group_id: int | None = None,
) -> TroubleTicket:
    """Create and persist a TroubleTicket for testing."""
    t = TroubleTicket(
        ticket_number=ticket_number,
        title=title,
        description=description,
        status=status,
        source=source,
        ai_summary=ai_summary,
        root_cause_group_id=root_cause_group_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_group(db: Session, *, title: str = "Auth Errors", suggested_fix: str | None = None) -> RootCauseGroup:
    """Create and persist a RootCauseGroup for testing."""
    g = RootCauseGroup(
        title=title,
        suggested_fix=suggested_fix,
        created_at=datetime.now(timezone.utc),
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


# ── Workspace view ────────────────────────────────────────────────────


class TestWorkspacePartial:
    def test_workspace_returns_html(self, db_session: Session, test_user: User):
        """GET /v2/partials/trouble-tickets/workspace returns 200 HTML."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/workspace")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "Analyze" in r.text

    def test_workspace_has_filter_pills(self, db_session: Session, test_user: User):
        """Workspace includes All / Open / Resolved / Won't Fix filter pills."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/workspace")
        assert r.status_code == 200
        assert "All" in r.text
        assert "Open" in r.text
        assert "Resolved" in r.text

    def test_workspace_has_analyze_button(self, db_session: Session, test_user: User):
        """Workspace includes the Analyze button wired to the analyze endpoint."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/workspace")
        assert r.status_code == 200
        assert "Analyze" in r.text
        assert "/api/trouble-tickets/analyze" in r.text

    def test_workspace_has_lazy_load_list(self, db_session: Session, test_user: User):
        """Workspace has #ticket-list div wired to auto-load the list partial."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/workspace")
        assert r.status_code == 200
        assert "ticket-list" in r.text
        assert "/v2/partials/trouble-tickets/list" in r.text


# ── List partial ──────────────────────────────────────────────────────


class TestListPartial:
    def test_list_empty(self, db_session: Session, test_user: User):
        """Empty list shows 'No tickets found' message."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/list")
        assert r.status_code == 200
        assert "No tickets found" in r.text

    def test_list_shows_tickets(self, db_session: Session, test_user: User):
        """Tickets from source='report_button' appear in list."""
        _make_ticket(db_session, title="Login fails on Chrome")
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/list")
        assert r.status_code == 200
        assert "Login fails on Chrome" in r.text

    def test_list_shows_ticket_count(self, db_session: Session, test_user: User):
        """List header shows correct ticket count."""
        _make_ticket(db_session, ticket_number="TT-0001")
        _make_ticket(db_session, ticket_number="TT-0002")
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/list")
        assert r.status_code == 200
        assert "2 tickets" in r.text

    def test_list_filter_by_status(self, db_session: Session, test_user: User):
        """Status filter hides tickets that don't match."""
        _make_ticket(db_session, ticket_number="TT-0001", status="submitted", title="Open ticket")
        _make_ticket(db_session, ticket_number="TT-0002", status="resolved", title="Resolved ticket")
        c = _make_client(db_session, test_user)

        r = c.get("/v2/partials/trouble-tickets/list?status=submitted")
        assert r.status_code == 200
        assert "Open ticket" in r.text
        assert "Resolved ticket" not in r.text

    def test_list_excludes_non_report_button_source(self, db_session: Session, test_user: User):
        """Tickets with source != 'report_button' are excluded from management list."""
        _make_ticket(db_session, ticket_number="TT-0001", source="ticket_form", title="Internal ticket")
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/list")
        assert r.status_code == 200
        assert "Internal ticket" not in r.text

    def test_list_grouped_by_root_cause(self, db_session: Session, test_user: User):
        """Tickets assigned to a root cause group appear under that group heading."""
        group = _make_group(db_session, title="Auth Errors", suggested_fix="Fix token refresh")
        _make_ticket(
            db_session,
            ticket_number="TT-0001",
            title="Token expired error",
            root_cause_group_id=group.id,
        )
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/list")
        assert r.status_code == 200
        assert "Auth Errors" in r.text
        assert "Token expired error" in r.text

    def test_list_shows_suggested_fix(self, db_session: Session, test_user: User):
        """Group suggested_fix text is displayed in the group header."""
        group = _make_group(db_session, title="DB Timeouts", suggested_fix="Add connection pooling")
        _make_ticket(db_session, ticket_number="TT-0001", root_cause_group_id=group.id)
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/list")
        assert r.status_code == 200
        assert "Add connection pooling" in r.text

    def test_list_ungrouped_tickets_appear(self, db_session: Session, test_user: User):
        """Tickets without a group appear in the ungrouped section."""
        _make_ticket(db_session, ticket_number="TT-0001", title="Orphan ticket")
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/list")
        assert r.status_code == 200
        assert "Orphan ticket" in r.text

    def test_list_shows_ai_summary_if_present(self, db_session: Session, test_user: User):
        """When ai_summary is set, it is shown instead of title in the row."""
        _make_ticket(
            db_session,
            ticket_number="TT-0001",
            title="Raw title",
            ai_summary="AI-generated summary text",
        )
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/list")
        assert r.status_code == 200
        assert "AI-generated summary text" in r.text

    def test_list_row_links_to_detail(self, db_session: Session, test_user: User):
        """Each row links to the ticket detail via HTMX hx-get."""
        t = _make_ticket(db_session, ticket_number="TT-0001")
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/list")
        assert r.status_code == 200
        assert f"/v2/partials/trouble-tickets/{t.id}" in r.text


# ── Detail partial ────────────────────────────────────────────────────


class TestDetailPartial:
    def test_detail_returns_html(self, db_session: Session, test_user: User):
        """GET /v2/partials/trouble-tickets/{id} returns 200 HTML."""
        t = _make_ticket(db_session, ticket_number="TT-0001")
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_detail_not_found(self, db_session: Session, test_user: User):
        """Nonexistent ticket returns 404."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/partials/trouble-tickets/99999")
        assert r.status_code == 404

    def test_detail_shows_ticket_number(self, db_session: Session, test_user: User):
        """Detail view shows the ticket number."""
        t = _make_ticket(db_session, ticket_number="TT-9999")
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "TT-9999" in r.text

    def test_detail_shows_description(self, db_session: Session, test_user: User):
        """Detail view shows the full description."""
        t = _make_ticket(db_session, ticket_number="TT-0001", description="Detailed bug description here")
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "Detailed bug description here" in r.text

    def test_detail_shows_ai_summary(self, db_session: Session, test_user: User):
        """Detail view shows AI summary when present."""
        t = _make_ticket(
            db_session,
            ticket_number="TT-0001",
            ai_summary="User cannot log in due to expired token",
        )
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "User cannot log in due to expired token" in r.text
        assert "AI Summary" in r.text

    def test_detail_no_ai_summary_section_when_absent(self, db_session: Session, test_user: User):
        """Detail view does not show AI Summary section when field is None."""
        t = _make_ticket(db_session, ticket_number="TT-0001", ai_summary=None)
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "AI Summary" not in r.text

    def test_detail_shows_root_cause_group(self, db_session: Session, test_user: User):
        """Detail view shows root cause group when assigned."""
        group = _make_group(db_session, title="Network Failures")
        t = _make_ticket(db_session, ticket_number="TT-0001", root_cause_group_id=group.id)
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "Network Failures" in r.text

    def test_detail_has_status_dropdown(self, db_session: Session, test_user: User):
        """Detail view includes a status select element."""
        t = _make_ticket(db_session, ticket_number="TT-0001", status="submitted")
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "<select" in r.text
        assert "submitted" in r.text
        assert "resolved" in r.text

    def test_detail_has_back_link(self, db_session: Session, test_user: User):
        """Detail view includes a 'Back to Tickets' link."""
        t = _make_ticket(db_session, ticket_number="TT-0001")
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "Back to Tickets" in r.text
        assert "/v2/partials/trouble-tickets/workspace" in r.text

    def test_detail_shows_console_errors_section(self, db_session: Session, test_user: User):
        """Detail view shows JS Errors section when console_errors is populated."""
        t = _make_ticket(db_session, ticket_number="TT-0001")
        t.console_errors = "TypeError: Cannot read property 'foo' of undefined"
        db_session.commit()
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "JS Errors" in r.text
        assert "TypeError" in r.text

    def test_detail_shows_page_url(self, db_session: Session, test_user: User):
        """Detail view shows the current_page URL in the captured context section."""
        t = _make_ticket(db_session, ticket_number="TT-0001")
        t.current_page = "/v2/requisitions/42"
        db_session.commit()
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/partials/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "/v2/requisitions/42" in r.text


# ── Full page (direct URL load) ───────────────────────────────────────


class TestFullPageLoad:
    def test_tickets_page_returns_html(self, db_session: Session, test_user: User):
        """GET /v2/trouble-tickets returns full page HTML."""
        c = _make_client(db_session, test_user)
        r = c.get("/v2/trouble-tickets")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_ticket_detail_page_returns_html(self, db_session: Session, test_user: User):
        """GET /v2/trouble-tickets/{id} (full page) returns 200 HTML."""
        t = _make_ticket(db_session, ticket_number="TT-0001")
        c = _make_client(db_session, test_user)
        r = c.get(f"/v2/trouble-tickets/{t.id}")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

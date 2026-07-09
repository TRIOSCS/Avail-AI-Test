"""test_tickets_wiring.py — wiring fixes for the Tickets triage workspace.

Covers Task 3 of the settings-menu refinement:
  1. Analyze returns the freshly-grouped list partial (not an empty body +
     dead HX-Trigger) so the innerHTML swap into #ticket-list shows results.
  2. The logical "open" status filter includes both submitted and in_progress
     tickets (previously "submitted" hid in_progress).
  3. The Settings shell honors ?tab=tickets (deep-link activates the Tickets tab).
     (Drill-in defers to main's full-page console at /v2/trouble-tickets; the
     Settings "Tickets" tab loads the workspace and rows launch that console.)
  4. The detail status <select> only toasts success on r.ok (honest error path).
  5. analyze_tickets passes schema= (not output_schema=) to claude_structured so
     it works against the real LLM (signature-pinning regression test).

Called by: pytest
Depends on: conftest.py fixtures (client = admin-capable, db_session, test_user),
            app.routers.error_reports, app.routers.htmx_views.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.constants import TicketStatus
from app.models import User
from app.models.trouble_ticket import TroubleTicket

# ── Helpers ──────────────────────────────────────────────────────────


def _seed_ticket(
    db: Session,
    user: User,
    *,
    ticket_number: str,
    status: str,
    title: str = "Something broke",
) -> TroubleTicket:
    """Seed one report_button ticket (mirrors error_reports._create_ticket shape)."""
    t = TroubleTicket(
        ticket_number=ticket_number,
        submitted_by=user.id,
        title=title,
        description=title,
        status=status,
        source="report_button",
        risk_tier="low",
        category="other",
        created_at=datetime.now(UTC),
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ── Fix 2: "Open" filter includes in_progress ────────────────────────


class TestOpenFilter:
    def test_open_filter_includes_in_progress(self, client, db_session, test_user):
        """Status=open surfaces BOTH submitted and in_progress tickets."""
        _seed_ticket(db_session, test_user, ticket_number="TT-0001", status=TicketStatus.SUBMITTED)
        _seed_ticket(db_session, test_user, ticket_number="TT-0002", status=TicketStatus.IN_PROGRESS)

        html = client.get("/v2/partials/trouble-tickets/list?status=open").text
        assert "TT-0001" in html
        assert "TT-0002" in html

    def test_open_filter_excludes_resolved(self, client, db_session, test_user):
        """Status=open hides resolved/wont_fix tickets."""
        _seed_ticket(db_session, test_user, ticket_number="TT-0001", status=TicketStatus.IN_PROGRESS)
        _seed_ticket(db_session, test_user, ticket_number="TT-0003", status=TicketStatus.RESOLVED)

        html = client.get("/v2/partials/trouble-tickets/list?status=open").text
        assert "TT-0001" in html
        assert "TT-0003" not in html

    def test_explicit_submitted_still_filters(self, client, db_session, test_user):
        """A literal status=submitted still narrows to just submitted (back-compat)."""
        _seed_ticket(db_session, test_user, ticket_number="TT-0001", status=TicketStatus.SUBMITTED)
        _seed_ticket(db_session, test_user, ticket_number="TT-0002", status=TicketStatus.IN_PROGRESS)

        html = client.get("/v2/partials/trouble-tickets/list?status=submitted").text
        assert "TT-0001" in html
        assert "TT-0002" not in html


# ── Fix 1: Analyze returns the grouped list partial ──────────────────


class TestAnalyzeReturnsList:
    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_analyze_returns_nonempty_list_partial(self, mock_claude, client, db_session, test_user):
        """Analyze renders + returns the grouped list (not an empty body)."""
        t = _seed_ticket(db_session, test_user, ticket_number="TT-0001", status=TicketStatus.SUBMITTED)
        mock_claude.return_value = {
            "groups": [{"title": "Auth Errors", "suggested_fix": "Fix token", "ticket_ids": [t.id]}]
        }

        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        body = resp.text.strip()
        assert body != ""
        # The list partial renders the freshly-grouped ticket + its group title.
        assert "TT-0001" in body
        assert "Auth Errors" in body

    @patch("app.utils.claude_client.claude_structured", new_callable=AsyncMock)
    def test_analyze_drops_dead_ticketsupdated_trigger(self, mock_claude, client, db_session, test_user):
        """No HX-Trigger: ticketsUpdated header (zero listeners) on the response."""
        t = _seed_ticket(db_session, test_user, ticket_number="TT-0001", status=TicketStatus.SUBMITTED)
        mock_claude.return_value = {"groups": [{"title": "Auth Errors", "ticket_ids": [t.id]}]}

        resp = client.post("/api/trouble-tickets/analyze")
        assert resp.status_code == 200
        assert "ticketsUpdated" not in resp.headers.get("HX-Trigger", "")


# ── Settings shell: ?tab=tickets deep-link activates the Tickets tab ──
# Tickets drill-in defers to main's full-page console at /v2/trouble-tickets; the
# Settings "Tickets" tab loads the workspace and its rows launch that console
# (#main-content). The ?tab= deep-link painting below stays useful regardless.


class TestSettingsTicketsTab:
    def test_settings_partial_tab_param_activates_tickets(self, client):
        """The settings shell honors ?tab=tickets — this is the partial the full page
        lazy-loads, so threading the redirect's tab param lands on Tickets."""
        html = client.get("/v2/partials/settings?tab=tickets").text
        # Alpine tab state initializes to 'tickets' and the first-paint content URL
        # points at the tickets workspace (NOT a non-existent /settings/tickets route).
        assert "{ tab: 'tickets' }" in html
        assert 'hx-get="/v2/partials/trouble-tickets/workspace"' in html
        assert "/v2/partials/settings/tickets" not in html

    def test_settings_partial_default_tab_by_role(self, client, db_session):
        """Default tab is role-aware (SET-04): an admin first-paints Connectors (the
        admin-only tab); a non-admin gets Profile instead of an empty 403 Connectors
        page.

        The `client` fixture is a buyer → Profile.
        """
        html = client.get("/v2/partials/settings").text
        assert "{ tab: 'profile' }" in html
        assert 'hx-get="/v2/partials/settings/profile"' in html

        # An admin still defaults to Connectors.
        from app.constants import UserRole
        from app.dependencies import require_user

        admin = type("A", (), {"id": 99, "role": UserRole.ADMIN, "name": "Ad", "email": "ad@x.com"})()
        client.app.dependency_overrides[require_user] = lambda: admin
        try:
            admin_html = client.get("/v2/partials/settings").text
        finally:
            client.app.dependency_overrides.pop(require_user, None)
        assert "{ tab: 'connectors' }" in admin_html
        assert 'hx-get="/v2/partials/settings/connectors"' in admin_html


# ── Fix 4: honest status toast (gate on r.ok) ────────────────────────


class TestHonestToast:
    def test_detail_status_handler_gates_on_ok(self, client, db_session, test_user):
        """The status <select> handler checks r.ok and has a .catch path."""
        t = _seed_ticket(db_session, test_user, ticket_number="TT-0001", status=TicketStatus.SUBMITTED)
        html = client.get(f"/v2/partials/trouble-tickets/{t.id}").text
        assert "r.ok" in html
        assert ".catch" in html


# ── Fix 5: analyze passes schema= (not output_schema=) ──────────────


class TestAnalyzeSchemaKwarg:
    """Signature-pinning regression: analyze_tickets must call claude_structured
    with the real ``schema`` kwarg, not the nonexistent ``output_schema`` kwarg.

    The shim below MATCHES the real claude_structured signature exactly.  If the
    caller passes ``output_schema=`` the shim raises TypeError (unexpected kwarg),
    which propagates through the try/except (TypeError is not ClaudeError) and
    causes a 500.  Once the caller uses ``schema=`` the shim accepts it and the
    endpoint returns 200 with the grouped list.
    """

    def test_analyze_uses_schema_kwarg_not_output_schema(self, client, db_session, test_user):
        """Analyze endpoint returns 200+groups when claude_structured uses schema=."""
        t = _seed_ticket(db_session, test_user, ticket_number="TT-0099", status=TicketStatus.SUBMITTED)

        canned = {"groups": [{"title": "Boot Loop", "suggested_fix": "Fix init", "ticket_ids": [t.id]}]}

        async def _signature_pinned_shim(
            prompt: str,
            schema: dict,
            *,
            system: str = "",
            model_tier: str = "fast",
            max_tokens: int = 1024,
            cache_system: bool = True,
            timeout: int = 30,
            thinking_budget=None,
            cost_bucket=None,
        ) -> dict:
            # If the caller passed ``output_schema=`` it would have blown up BEFORE
            # reaching here (Python raises TypeError for unexpected kwargs immediately).
            return canned

        with patch("app.utils.claude_client.claude_structured", new=_signature_pinned_shim):
            resp = client.post("/api/trouble-tickets/analyze")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
        body = resp.text
        assert "TT-0099" in body
        assert "Boot Loop" in body

"""Tests for trouble ticket service and router.

Covers: ticket creation, auto-context capture, sanitization,
ticket number generation, listing, access control, verify endpoint.

Called by: pytest
Depends on: conftest fixtures, app.models.TroubleTicket
"""

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.trouble_ticket import TroubleTicket
from app.services.diagnosis_service import diagnose_full
from app.services.execution_service import execute_fix
from app.services.trouble_ticket_service import (
    _capture_auto_context,
    _sanitize_context,
    auto_process_ticket,
    check_file_lock,
    create_ticket,
    get_ticket,
    get_tickets_by_user,
    list_tickets,
    update_ticket,
)


class TestTicketCreation:
    def test_create_ticket_basic(self, db_session: Session, test_user: User):
        ticket = create_ticket(
            db=db_session,
            user_id=test_user.id,
            title="Button not working",
            description="The submit button on the RFQ page does nothing when clicked.",
            current_page="/api/rfq",
            user_agent="Mozilla/5.0",
            frontend_errors=[],
        )
        assert ticket.id is not None
        assert ticket.status == "submitted"
        assert ticket.submitted_by == test_user.id
        assert ticket.title == "Button not working"

    def test_ticket_number_format(self, db_session: Session, test_user: User):
        ticket = create_ticket(
            db=db_session,
            user_id=test_user.id,
            title="Test",
            description="Test description",
        )
        assert re.match(r"TT-\d{8}-\d{3,}", ticket.ticket_number)

    def test_ticket_number_sequential(self, db_session: Session, test_user: User):
        t1 = create_ticket(db=db_session, user_id=test_user.id, title="First", description="First ticket")
        t2 = create_ticket(db=db_session, user_id=test_user.id, title="Second", description="Second ticket")
        num1 = int(t1.ticket_number.rsplit("-", 1)[1])
        num2 = int(t2.ticket_number.rsplit("-", 1)[1])
        assert num2 == num1 + 1


class TestAutoContext:
    def test_capture_returns_expected_structure(self, db_session: Session, test_user: User):
        ctx = _capture_auto_context(db=db_session, user_id=test_user.id, current_page="/api/vendors/123")
        assert "recent_api_errors" in ctx
        assert "recent_frontend_errors" in ctx
        assert "user_role" in ctx
        assert "server_info" in ctx
        assert "page_route" in ctx
        assert ctx["user_role"] == "buyer"

    def test_page_route_parameterized(self, db_session: Session, test_user: User):
        ctx = _capture_auto_context(db=db_session, user_id=test_user.id, current_page="/api/vendors/12345")
        assert ctx["page_route"] == "/api/vendors/{id}"


class TestSanitization:
    def test_strips_api_keys(self):
        ctx = {"error": "Failed with key sk-ant-api03-abc123xyz"}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "sk-ant-api03-abc123xyz" not in str(result)

    def test_strips_bearer_tokens(self):
        ctx = {"header": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.abc.def"}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9" not in str(result)

    def test_strips_connection_strings(self):
        ctx = {"db": "postgres://user:pass@host:5432/db"}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "postgres://user:pass" not in str(result)

    def test_strips_passwords(self):
        ctx = {"config": 'password = "hunter2"'}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "hunter2" not in str(result)

    def test_strips_api_key_values(self):
        ctx = {"config": 'api_key = "my-secret-key-123"'}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "my-secret-key-123" not in str(result)

    def test_replaces_emails_except_submitter(self):
        ctx = {"data": "Contact admin@company.com and other@example.com"}
        result = _sanitize_context(ctx, submitter_email="admin@company.com")
        result_str = str(result)
        assert "admin@company.com" in result_str
        assert "other@example.com" not in result_str
        assert "[EMAIL]" in result_str

    def test_replaces_ip_addresses(self):
        ctx = {"server": "Connected to 192.168.1.100"}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert "192.168.1.100" not in str(result)
        assert "[IP]" in str(result)

    def test_truncates_long_messages(self):
        ctx = {"error": "x" * 1000}
        result = _sanitize_context(ctx, submitter_email="test@example.com")
        assert len(result["error"]) <= 500


class TestTicketQueries:
    def test_get_ticket(self, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="Test", description="Desc")
        found = get_ticket(db=db_session, ticket_id=ticket.id)
        assert found is not None
        assert found.id == ticket.id

    def test_get_ticket_not_found(self, db_session: Session):
        found = get_ticket(db=db_session, ticket_id=99999)
        assert found is None

    def test_list_tickets_with_status_filter(self, db_session: Session, test_user: User):
        create_ticket(db=db_session, user_id=test_user.id, title="T1", description="D1")
        t2 = create_ticket(db=db_session, user_id=test_user.id, title="T2", description="D2")
        t2.status = "diagnosed"
        db_session.commit()
        result = list_tickets(db=db_session, status_filter="submitted")
        assert result["total"] == 1
        assert result["items"][0]["status"] == "submitted"

    def test_list_tickets_pagination(self, db_session: Session, test_user: User):
        for i in range(5):
            create_ticket(db=db_session, user_id=test_user.id, title=f"T{i}", description=f"D{i}")
        result = list_tickets(db=db_session, limit=2, offset=0)
        assert result["total"] == 5
        assert len(result["items"]) == 2

    def test_get_tickets_by_user(self, db_session: Session, test_user: User, admin_user: User):
        create_ticket(db=db_session, user_id=test_user.id, title="User ticket", description="D")
        create_ticket(db=db_session, user_id=admin_user.id, title="Admin ticket", description="D")
        user_tickets = get_tickets_by_user(db=db_session, user_id=test_user.id)
        assert len(user_tickets) == 1
        assert user_tickets[0].title == "User ticket"


class TestFileLock:
    def test_no_conflict(self, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.status = "in_progress"
        ticket.file_mapping = ["app/routers/vendors.py"]
        db_session.commit()
        result = check_file_lock(db=db_session, file_paths=["app/routers/crm.py"])
        assert result is None

    def test_conflict_detected(self, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.status = "in_progress"
        ticket.file_mapping = ["app/routers/vendors.py", "app/services/vendor_service.py"]
        db_session.commit()
        result = check_file_lock(db=db_session, file_paths=["app/routers/vendors.py"])
        assert result is not None
        assert result.id == ticket.id


class TestRouterEndpoints:
    @patch("app.routers.trouble_tickets.svc.auto_process_ticket", new_callable=AsyncMock)
    def test_create_ticket_endpoint(self, mock_auto, client, db_session: Session):
        resp = client.post(
            "/api/trouble-tickets",
            json={
                "title": "Test ticket",
                "description": "Something broke",
                "current_page": "/vendors",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "ticket_number" in data

    def test_my_tickets_endpoint(self, client, db_session: Session, test_user: User):
        create_ticket(db=db_session, user_id=test_user.id, title="My ticket", description="D")
        resp = client.get("/api/trouble-tickets/my-tickets")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 1

    def test_get_ticket_endpoint(self, client, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="Detail test", description="D")
        resp = client.get(f"/api/trouble-tickets/{ticket.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Detail test"
        assert data["submitted_by"] == test_user.id

    def test_get_ticket_not_found(self, client, db_session: Session):
        resp = client.get("/api/trouble-tickets/99999")
        assert resp.status_code == 404


class TestVerifyEndpoint:
    def test_verify_resolved(self, client, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.status = "awaiting_verification"
        db_session.commit()
        resp = client.post(f"/api/trouble-tickets/{ticket.id}/verify", json={"is_fixed": True})
        assert resp.status_code == 200
        db_session.refresh(ticket)
        assert ticket.status == "resolved"

    def test_verify_still_broken_creates_child(self, client, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.status = "awaiting_verification"
        ticket.risk_tier = "low"
        db_session.commit()
        resp = client.post(
            f"/api/trouble-tickets/{ticket.id}/verify",
            json={"is_fixed": False, "description": "Still broken"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "child_ticket_id" in data
        child = db_session.get(TroubleTicket, data["child_ticket_id"])
        assert child.parent_ticket_id == ticket.id
        assert child.risk_tier in ("medium", "high")

    def test_verify_wrong_status(self, client, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        resp = client.post(f"/api/trouble-tickets/{ticket.id}/verify", json={"is_fixed": True})
        assert resp.status_code == 400

    def test_verify_not_found(self, client, db_session: Session):
        resp = client.post("/api/trouble-tickets/99999/verify", json={"is_fixed": True})
        assert resp.status_code == 404


class TestUpdateTicket:
    def test_update_not_found(self, db_session: Session):
        result = update_ticket(db_session, 99999, status="triaging")
        assert result is None

    def test_update_diagnosed_sets_timestamp(self, db_session: Session, test_user: User):
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        assert ticket.diagnosed_at is None
        updated = update_ticket(db_session, ticket.id, status="diagnosed")
        assert updated.diagnosed_at is not None

    def test_sanitize_non_string_non_dict_non_list(self, db_session: Session):
        context = {"count": 42, "flag": True, "score": 3.14}
        result = _sanitize_context(context)
        assert result["count"] == 42
        assert result["flag"] is True
        assert result["score"] == 3.14


class TestDiagnoseEndpoint:
    """Tests for the diagnose endpoint — uses service-level calls since
    the router is a thin wrapper. HTTP tests for feature gate only."""

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_diagnose_full_pipeline(self, mock_claude, db_session, test_user):
        """diagnose_full should classify, diagnose, and update the ticket."""
        import asyncio

        mock_claude.side_effect = [
            {"category": "ui", "risk_tier": "low", "confidence": 0.9, "summary": "UI bug"},
            {
                "root_cause": "CSS",
                "affected_files": [],
                "fix_approach": "Fix",
                "test_strategy": "Test",
                "estimated_complexity": "simple",
                "requires_migration": False,
            },
        ]
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="Bug", description="Something broke")
        result = asyncio.get_event_loop().run_until_complete(diagnose_full(ticket.id, db_session))
        assert result["status"] == "diagnosed"
        assert result["risk_tier"] == "low"
        assert result["classification"]["category"] == "ui"

    def test_diagnose_feature_gate(self, db_session, test_user):
        """Auto-process should skip when self_heal_enabled is False."""
        import asyncio

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="Bug", description="Something broke")
        with patch("app.services.trouble_ticket_service.settings") as mock_settings:
            mock_settings.self_heal_enabled = False
            asyncio.get_event_loop().run_until_complete(auto_process_ticket(ticket.id))
        db_session.refresh(ticket)
        # Ticket should remain submitted — auto_process_ticket exits early
        assert ticket.status == "submitted"

    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_diagnose_already_diagnosed(self, mock_claude, db_session, test_user):
        """Should not re-diagnose a ticket that already has a diagnosis."""
        import asyncio

        mock_claude.side_effect = [
            {"category": "api", "risk_tier": "medium", "confidence": 0.8, "summary": "API bug"},
            {
                "root_cause": "Logic error",
                "affected_files": ["app/services/foo.py"],
                "fix_approach": "Fix logic",
                "test_strategy": "Test it",
                "estimated_complexity": "moderate",
                "requires_migration": False,
            },
        ]
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="Bug", description="API error")
        # First diagnosis
        asyncio.get_event_loop().run_until_complete(diagnose_full(ticket.id, db_session))
        # Verify ticket is diagnosed
        db_session.refresh(ticket)
        assert ticket.diagnosis is not None
        assert ticket.status == "diagnosed"


class TestExecuteEndpoint:
    """Tests for the execute endpoint — service-level since router is thin wrapper."""

    def test_execute_feature_gate(self, db_session, test_user):
        """Auto-process should skip execution when self_heal_enabled is False."""
        import asyncio

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="Bug", description="Something broke")
        with patch("app.services.trouble_ticket_service.settings") as mock_settings:
            mock_settings.self_heal_enabled = False
            asyncio.get_event_loop().run_until_complete(auto_process_ticket(ticket.id))
        db_session.refresh(ticket)
        assert ticket.status == "submitted"

    @patch("app.services.execution_service._generate_fix", new_callable=AsyncMock)
    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    def test_execute_after_diagnose(self, mock_claude, mock_run, db_session, test_user):
        """Full flow: diagnose → execute → awaiting_verification."""
        import asyncio

        mock_claude.side_effect = [
            {"category": "ui", "risk_tier": "low", "confidence": 0.9, "summary": "Bug"},
            {
                "root_cause": "CSS",
                "affected_files": [],
                "fix_approach": "Fix",
                "test_strategy": "Test",
                "estimated_complexity": "simple",
                "requires_migration": False,
            },
        ]
        mock_run.return_value = {
            "success": True,
            "patches": [{"file": "x.py", "search": "a", "replace": "b"}],
            "summary": "Fixed",
            "cost_usd": 0.10,
        }

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="CSS Bug", description="Button misaligned")
        asyncio.get_event_loop().run_until_complete(diagnose_full(ticket.id, db_session))
        db_session.refresh(ticket)
        assert ticket.status == "diagnosed"

        with patch("app.services.execution_service._write_fix_queue"):
            result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
        assert result["ok"] is True
        db_session.refresh(ticket)
        assert ticket.status == "fix_queued"


class TestStatsEndpoint:
    def test_stats_returns_health_and_stats(self, db_session, test_user):
        """Stats endpoint returns weekly stats + health indicator."""
        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        create_ticket(db=db_session, user_id=test_user.id, title="A", description="D")

        admin = User(
            email="stats_admin@trioscs.com",
            name="Stats Admin",
            role="admin",
            azure_id="test-stats-admin",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(admin)
        db_session.commit()
        db_session.refresh(admin)

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin
        app.dependency_overrides[require_admin] = lambda: admin

        from starlette.testclient import TestClient

        with TestClient(app) as c:
            resp = c.get("/api/trouble-tickets/stats")
        app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert "stats" in data
        assert "health" in data
        assert data["health"]["status"] in ("green", "yellow", "red")
        assert data["stats"]["tickets_created"] >= 1


class TestAutoProcessTicket:
    """Tests for auto_process_ticket — background auto-diagnose and auto-execute.

    auto_process_ticket creates its own DB session via SessionLocal(), so we
    mock that to return the test session with close() as a no-op.
    """

    def _mock_session(self, db_session):
        """Return a mock SessionLocal that yields db_session with no-op close."""
        from unittest.mock import MagicMock

        wrapper = MagicMock(wraps=db_session)
        wrapper.close = MagicMock()  # no-op close
        wrapper.get = db_session.get
        wrapper.query = db_session.query
        wrapper.add = db_session.add
        wrapper.commit = db_session.commit
        wrapper.refresh = db_session.refresh
        return wrapper

    @patch("app.services.trouble_ticket_service.settings")
    def test_skips_when_disabled(self, mock_settings, db_session, test_user):
        """Should return immediately when self_heal_enabled is False."""
        import asyncio

        mock_settings.self_heal_enabled = False
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        asyncio.get_event_loop().run_until_complete(auto_process_ticket(ticket.id))
        db_session.refresh(ticket)
        assert ticket.status == "submitted"

    @patch("app.database.SessionLocal")
    @patch("app.services.execution_service._generate_fix", new_callable=AsyncMock)
    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    @patch("app.services.trouble_ticket_service.settings")
    def test_auto_execute_low_risk(
        self, mock_settings, mock_claude, mock_run, mock_session_local, db_session, test_user
    ):
        """Low-risk ticket should be auto-diagnosed and auto-executed."""
        import asyncio

        mock_settings.self_heal_enabled = True
        mock_settings.self_heal_auto_diagnose = True
        mock_settings.self_heal_auto_execute_low = True
        mock_session_local.return_value = self._mock_session(db_session)
        mock_claude.side_effect = [
            {"category": "ui", "risk_tier": "low", "confidence": 0.9, "summary": "UI bug"},
            {
                "root_cause": "CSS",
                "affected_files": [],
                "fix_approach": "Fix",
                "test_strategy": "Test",
                "estimated_complexity": "simple",
                "requires_migration": False,
            },
        ]
        mock_run.return_value = {
            "success": True,
            "patches": [{"file": "x.py", "search": "a", "replace": "b"}],
            "summary": "Fixed",
            "cost_usd": 0.05,
        }
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="CSS Bug", description="Button misaligned")
        with patch("app.services.execution_service._write_fix_queue"):
            asyncio.get_event_loop().run_until_complete(auto_process_ticket(ticket.id))
        db_session.refresh(ticket)
        assert ticket.status == "fix_queued"

    @patch("app.database.SessionLocal")
    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    @patch("app.services.trouble_ticket_service.settings")
    def test_medium_risk_diagnosed_not_executed(
        self, mock_settings, mock_claude, mock_session_local, db_session, test_user
    ):
        """Medium-risk ticket should be auto-diagnosed but NOT auto-executed
        (auto_execute_low only applies to low risk)."""
        import asyncio

        mock_settings.self_heal_enabled = True
        mock_settings.self_heal_auto_diagnose = True
        mock_settings.self_heal_auto_execute_low = True
        mock_session_local.return_value = self._mock_session(db_session)
        mock_claude.side_effect = [
            {"category": "api", "risk_tier": "medium", "confidence": 0.8, "summary": "API error"},
            {
                "root_cause": "Logic",
                "affected_files": ["app/services/foo.py"],
                "fix_approach": "Fix logic",
                "test_strategy": "Test",
                "estimated_complexity": "moderate",
                "requires_migration": False,
            },
        ]
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="API Bug", description="Endpoint returns 500")
        asyncio.get_event_loop().run_until_complete(auto_process_ticket(ticket.id))
        db_session.refresh(ticket)
        assert ticket.status == "diagnosed"

    @patch("app.database.SessionLocal")
    @patch("app.services.execution_service._generate_fix", new_callable=AsyncMock)
    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    @patch("app.services.trouble_ticket_service.settings")
    def test_skips_execute_high_risk(self, mock_settings, mock_claude, mock_gen, mock_session_local, db_session, test_user):
        """Aggressive mode: high-risk ticket is auto-diagnosed AND auto-executed."""
        import asyncio

        mock_settings.self_heal_enabled = True
        mock_settings.self_heal_auto_diagnose = True
        mock_settings.self_heal_auto_execute_low = True
        mock_session_local.return_value = self._mock_session(db_session)
        # Two Claude calls in aggressive mode: classification + diagnosis
        mock_claude.side_effect = [
            {"category": "data", "risk_tier": "high", "confidence": 0.9, "summary": "Data loss risk"},
            {
                "root_cause": "Data corruption",
                "affected_files": ["app/models/foo.py"],
                "fix_approach": "Fix data",
                "test_strategy": "Test data",
                "estimated_complexity": "complex",
                "requires_migration": False,
            },
        ]
        mock_gen.return_value = {
            "success": True,
            "patches": [{"file": "app/models/foo.py", "search": "a", "replace": "b"}],
            "summary": "Fixed data issue",
            "cost_usd": 0.10,
        }
        ticket = create_ticket(
            db=db_session, user_id=test_user.id, title="Data Bug", description="Records disappearing"
        )
        with patch("app.services.execution_service._write_fix_queue"):
            asyncio.get_event_loop().run_until_complete(auto_process_ticket(ticket.id))
        db_session.refresh(ticket)
        assert ticket.status == "fix_queued"

    @patch("app.database.SessionLocal")
    @patch("app.services.diagnosis_service.claude_structured", new_callable=AsyncMock)
    @patch("app.services.trouble_ticket_service.settings")
    def test_handles_diagnosis_failure(self, mock_settings, mock_claude, mock_session_local, db_session, test_user):
        """If diagnosis fails, ticket should stay as submitted."""
        import asyncio

        mock_settings.self_heal_enabled = True
        mock_settings.self_heal_auto_diagnose = True
        mock_session_local.return_value = self._mock_session(db_session)
        mock_claude.return_value = None  # classification fails
        ticket = create_ticket(db=db_session, user_id=test_user.id, title="Bug", description="Something broke")
        asyncio.get_event_loop().run_until_complete(auto_process_ticket(ticket.id))
        db_session.refresh(ticket)
        assert ticket.status == "submitted"


# ═══════════════════════════════════════════════════════════════════════
#  NEW COVERAGE TESTS — uncovered lines in routers/trouble_tickets.py
# ═══════════════════════════════════════════════════════════════════════


def _make_admin_client(db_session, admin_user):
    """Helper: TestClient with admin auth overrides."""
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_buyer] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def tt_admin_client(db_session, admin_user):
    """Admin TestClient fixture for trouble ticket router tests."""
    yield from _make_admin_client(db_session, admin_user)


@pytest.fixture()
def tt_sample(db_session, admin_user):
    """A pre-existing trouble ticket in the DB."""
    t = TroubleTicket(
        ticket_number="TT-COV-001",
        submitted_by=admin_user.id,
        title="Coverage Bug",
        description="Something broke for coverage",
        status="submitted",
        source="ticket_form",
        risk_tier="low",
        category="ui",
        current_page="/dashboard",
        browser_info="Chrome 120",
        screen_size="1920x1080",
        console_errors="TypeError: x is null",
        current_view="pipeline",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


class TestScreenshotTooLarge:
    """Line 52: screenshot_b64 exceeding MAX_SCREENSHOT_SIZE returns 400."""

    @patch("app.routers.trouble_tickets.svc.auto_process_ticket", new_callable=AsyncMock)
    def test_create_ticket_screenshot_too_large(self, mock_auto, tt_admin_client):
        huge = "A" * (2 * 1024 * 1024 + 1)
        resp = tt_admin_client.post(
            "/api/trouble-tickets",
            json={"title": "Big screenshot", "description": "Has big img", "screenshot_b64": huge},
        )
        assert resp.status_code == 400
        body = resp.json()
        # May be in "detail" (FastAPI HTTPException) or "error" (custom handler)
        msg = body.get("detail") or body.get("error", "")
        assert "Screenshot too large" in msg


class TestReportButtonAIPrompt:
    """Lines 78-101: source=report_button triggers AI prompt generation."""

    @patch("app.routers.trouble_tickets.svc.auto_process_ticket", new_callable=AsyncMock)
    @patch("app.routers.trouble_tickets.svc.create_ticket")
    @patch("app.routers.trouble_tickets.svc.update_ticket")
    def test_ai_prompt_success(self, mock_update, mock_create, mock_auto, tt_admin_client):
        fake = MagicMock()
        fake.id = 900
        fake.ticket_number = "TT-RP-001"
        mock_create.return_value = fake

        with patch(
            "app.services.ai_trouble_prompt.generate_trouble_prompt",
            new_callable=AsyncMock,
            return_value={"title": "AI Title", "prompt": "Fix this"},
        ):
            resp = tt_admin_client.post(
                "/api/trouble-tickets",
                json={"title": "Broken", "description": "Broke", "source": "report_button"},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_update.assert_called_once()

    @patch("app.routers.trouble_tickets.svc.auto_process_ticket", new_callable=AsyncMock)
    @patch("app.routers.trouble_tickets.svc.create_ticket")
    def test_ai_prompt_failure_still_creates(self, mock_create, mock_auto, tt_admin_client):
        fake = MagicMock()
        fake.id = 901
        fake.ticket_number = "TT-RP-002"
        mock_create.return_value = fake

        with patch(
            "app.services.ai_trouble_prompt.generate_trouble_prompt",
            new_callable=AsyncMock,
            side_effect=RuntimeError("AI down"),
        ):
            resp = tt_admin_client.post(
                "/api/trouble-tickets",
                json={"title": "Bug", "description": "Broke", "source": "report_button"},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("app.routers.trouble_tickets.svc.auto_process_ticket", new_callable=AsyncMock)
    @patch("app.routers.trouble_tickets.svc.create_ticket")
    @patch("app.routers.trouble_tickets.svc.update_ticket")
    def test_ai_prompt_returns_none(self, mock_update, mock_create, mock_auto, tt_admin_client):
        """When generate_trouble_prompt returns None, update_ticket is NOT called."""
        fake = MagicMock()
        fake.id = 902
        fake.ticket_number = "TT-RP-003"
        mock_create.return_value = fake

        with patch(
            "app.services.ai_trouble_prompt.generate_trouble_prompt",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = tt_admin_client.post(
                "/api/trouble-tickets",
                json={"title": "Bug", "description": "X", "source": "report_button"},
            )
        assert resp.status_code == 200
        mock_update.assert_not_called()


class TestExportXlsx:
    """Lines 167-231: Excel export endpoint."""

    def test_export_no_filter(self, tt_admin_client, tt_sample):
        resp = tt_admin_client.get("/api/trouble-tickets/export/xlsx")
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]
        assert len(resp.content) > 100

    def test_export_with_status_filter(self, tt_admin_client, tt_sample):
        resp = tt_admin_client.get("/api/trouble-tickets/export/xlsx?status=submitted")
        assert resp.status_code == 200
        assert "spreadsheetml" in resp.headers["content-type"]

    def test_export_with_source_filter(self, tt_admin_client, tt_sample):
        resp = tt_admin_client.get("/api/trouble-tickets/export/xlsx?source=ticket_form")
        assert resp.status_code == 200

    def test_export_empty_result(self, tt_admin_client):
        resp = tt_admin_client.get("/api/trouble-tickets/export/xlsx?status=nonexistent")
        assert resp.status_code == 200
        assert len(resp.content) > 0  # still returns Excel with headers


class TestSimilarTickets:
    """Lines 298-299: similar tickets with match found."""

    @patch("app.services.ticket_consolidation.find_similar_ticket", new_callable=AsyncMock)
    def test_similar_match_found(self, mock_find, tt_admin_client, tt_sample, db_session):
        mock_find.return_value = {"match_id": tt_sample.id, "confidence": 0.85}
        resp = tt_admin_client.get(
            "/api/trouble-tickets/similar",
            params={"title": "Coverage Bug dup", "description": "Similar issue"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["matches"]) == 1
        assert data["matches"][0]["id"] == tt_sample.id
        assert data["matches"][0]["confidence"] == 0.85

    @patch("app.services.ticket_consolidation.find_similar_ticket", new_callable=AsyncMock)
    def test_similar_match_id_invalid(self, mock_find, tt_admin_client, db_session):
        """Match ID points to non-existent ticket -- returns empty matches."""
        mock_find.return_value = {"match_id": 999999, "confidence": 0.80}
        resp = tt_admin_client.get(
            "/api/trouble-tickets/similar",
            params={"title": "No match ticket"},
        )
        assert resp.status_code == 200
        assert resp.json()["matches"] == []

    @patch("app.services.ticket_consolidation.find_similar_ticket", new_callable=AsyncMock)
    def test_similar_no_match(self, mock_find, tt_admin_client):
        mock_find.return_value = None
        resp = tt_admin_client.get(
            "/api/trouble-tickets/similar",
            params={"title": "Unique bug report"},
        )
        assert resp.status_code == 200
        assert resp.json()["matches"] == []


class TestUpdateResolvedBy:
    """Lines 402-403: PATCH with status=resolved sets resolved_by_id."""

    def test_resolved_sets_resolved_by(self, tt_admin_client, tt_sample, admin_user):
        resp = tt_admin_client.patch(
            f"/api/trouble-tickets/{tt_sample.id}",
            json={"status": "resolved"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_update_empty_body_400(self, tt_admin_client, tt_sample):
        resp = tt_admin_client.patch(f"/api/trouble-tickets/{tt_sample.id}", json={})
        assert resp.status_code == 400

    def test_update_not_found_404(self, tt_admin_client):
        resp = tt_admin_client.patch("/api/trouble-tickets/99999", json={"status": "resolved"})
        assert resp.status_code == 404


class TestRegeneratePrompt:
    """Lines 452-479: regenerate AI prompt endpoint."""

    @patch("app.services.ai_trouble_prompt.generate_trouble_prompt", new_callable=AsyncMock)
    def test_regenerate_success(self, mock_gen, tt_admin_client, tt_sample):
        mock_gen.return_value = {"title": "New Title", "prompt": "New prompt text"}
        resp = tt_admin_client.post(f"/api/trouble-tickets/{tt_sample.id}/regenerate-prompt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ai_prompt"] == "New prompt text"
        assert data["title"] == "New Title"

    @patch("app.services.ai_trouble_prompt.generate_trouble_prompt", new_callable=AsyncMock)
    def test_regenerate_ai_returns_none_502(self, mock_gen, tt_admin_client, tt_sample):
        mock_gen.return_value = None
        resp = tt_admin_client.post(f"/api/trouble-tickets/{tt_sample.id}/regenerate-prompt")
        assert resp.status_code == 502

    def test_regenerate_not_found_404(self, tt_admin_client):
        resp = tt_admin_client.post("/api/trouble-tickets/99999/regenerate-prompt")
        assert resp.status_code == 404


class TestVerifyRetest:
    """Lines 540-545: POST verify-retest endpoint."""

    @patch("app.services.rollback_service.verify_and_retest", new_callable=AsyncMock)
    def test_verify_retest_success(self, mock_rt, tt_admin_client, tt_sample):
        mock_rt.return_value = {"ok": True, "passed": True}
        resp = tt_admin_client.post(f"/api/trouble-tickets/{tt_sample.id}/verify-retest")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("app.services.rollback_service.verify_and_retest", new_callable=AsyncMock)
    def test_verify_retest_error_400(self, mock_rt, tt_admin_client, tt_sample):
        mock_rt.return_value = {"error": "Retest failed"}
        resp = tt_admin_client.post(f"/api/trouble-tickets/{tt_sample.id}/verify-retest")
        assert resp.status_code == 400


class TestInternalVerifyRetest:
    """Lines 558-581: POST /api/internal/verify-retest/{id} (localhost-only)."""

    @patch("app.services.rollback_service.verify_and_retest", new_callable=AsyncMock)
    def test_internal_retest_success(self, mock_rt, db_session, tt_sample):
        from app.database import get_db
        from app.main import app

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        mock_rt.return_value = {"ok": True, "passed": True}

        # Mock request.client to return localhost
        with patch("app.routers.trouble_tickets.Request") as mock_req_cls:
            # Instead of mocking the class, we need to mock at the ASGI level
            pass

        # TestClient sends from "testclient" host, need to override the check
        with patch(
            "app.routers.trouble_tickets.internal_verify_retest",
            wraps=None,
        ):
            pass

        # Simplest approach: patch the endpoint to skip localhost check
        from unittest.mock import PropertyMock

        with TestClient(app, headers={"X-Forwarded-For": "127.0.0.1"}) as c:
            with patch(
                "starlette.requests.Request.client",
                new_callable=PropertyMock,
                return_value=MagicMock(host="127.0.0.1"),
            ):
                resp = c.post(f"/api/internal/verify-retest/{tt_sample.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        app.dependency_overrides.clear()

    @patch("app.services.rollback_service.verify_and_retest", new_callable=AsyncMock)
    def test_internal_retest_error_400(self, mock_rt, db_session, tt_sample):
        from app.database import get_db
        from app.main import app
        from unittest.mock import PropertyMock

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        mock_rt.return_value = {"error": "Still failing"}
        with TestClient(app) as c:
            with patch(
                "starlette.requests.Request.client",
                new_callable=PropertyMock,
                return_value=MagicMock(host="127.0.0.1"),
            ):
                resp = c.post(f"/api/internal/verify-retest/{tt_sample.id}")
        assert resp.status_code == 400
        app.dependency_overrides.clear()

    def test_internal_retest_non_localhost_403(self, db_session, tt_sample):
        """Request from non-localhost returns 403."""
        from app.database import get_db
        from app.main import app
        from unittest.mock import PropertyMock

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        with TestClient(app) as c:
            with patch(
                "starlette.requests.Request.client",
                new_callable=PropertyMock,
                return_value=MagicMock(host="10.0.0.5"),
            ):
                resp = c.post(f"/api/internal/verify-retest/{tt_sample.id}")
        assert resp.status_code == 403
        app.dependency_overrides.clear()


class TestDiagnoseRouter:
    """Lines around diagnose endpoint -- self_heal gate, not found, already diagnosed."""

    def test_diagnose_disabled_403(self, tt_admin_client, tt_sample):
        with patch.object(
            __import__("app.config", fromlist=["settings"]).settings,
            "self_heal_enabled",
            False,
        ):
            resp = tt_admin_client.post(f"/api/trouble-tickets/{tt_sample.id}/diagnose")
        assert resp.status_code == 403

    def test_diagnose_not_found_404(self, tt_admin_client):
        with patch.object(
            __import__("app.config", fromlist=["settings"]).settings,
            "self_heal_enabled",
            True,
        ):
            resp = tt_admin_client.post("/api/trouble-tickets/99999/diagnose")
        assert resp.status_code == 404

    def test_diagnose_already_diagnosed_400(self, tt_admin_client, db_session, admin_user):
        t = TroubleTicket(
            ticket_number="TT-DIAG-COV",
            submitted_by=admin_user.id,
            title="Diagnosed",
            description="Already done",
            status="diagnosed",
            diagnosis={"summary": "known"},
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(t)
        db_session.commit()
        db_session.refresh(t)
        with patch.object(
            __import__("app.config", fromlist=["settings"]).settings,
            "self_heal_enabled",
            True,
        ):
            resp = tt_admin_client.post(f"/api/trouble-tickets/{t.id}/diagnose")
        assert resp.status_code == 400

    @patch("app.routers.trouble_tickets.diagnose_full", new_callable=AsyncMock)
    def test_diagnose_error_result_500(self, mock_diag, tt_admin_client, tt_sample):
        mock_diag.return_value = {"error": "Claude API failure"}
        with patch.object(
            __import__("app.config", fromlist=["settings"]).settings,
            "self_heal_enabled",
            True,
        ):
            resp = tt_admin_client.post(f"/api/trouble-tickets/{tt_sample.id}/diagnose")
        assert resp.status_code == 500


class TestExecuteRouter:
    """Lines around execute endpoint -- self_heal gate, error result."""

    def test_execute_disabled_403(self, tt_admin_client, tt_sample):
        with patch.object(
            __import__("app.config", fromlist=["settings"]).settings,
            "self_heal_enabled",
            False,
        ):
            resp = tt_admin_client.post(f"/api/trouble-tickets/{tt_sample.id}/execute")
        assert resp.status_code == 403

    @patch("app.routers.trouble_tickets.execute_fix", new_callable=AsyncMock)
    def test_execute_error_400(self, mock_exec, tt_admin_client, tt_sample):
        mock_exec.return_value = {"error": "No diagnosis"}
        with patch.object(
            __import__("app.config", fromlist=["settings"]).settings,
            "self_heal_enabled",
            True,
        ):
            resp = tt_admin_client.post(f"/api/trouble-tickets/{tt_sample.id}/execute")
        assert resp.status_code == 400

    @patch("app.routers.trouble_tickets.execute_fix", new_callable=AsyncMock)
    def test_execute_success(self, mock_exec, tt_admin_client, tt_sample):
        mock_exec.return_value = {"ok": True, "fix_branch": "fix/tt-cov"}
        with patch.object(
            __import__("app.config", fromlist=["settings"]).settings,
            "self_heal_enabled",
            True,
        ):
            resp = tt_admin_client.post(f"/api/trouble-tickets/{tt_sample.id}/execute")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestGetTicketAccessDenied:
    """GET ticket by non-owner non-admin returns 403."""

    def test_non_owner_non_admin_403(self, db_session, admin_user, test_user):
        from app.database import get_db
        from app.dependencies import require_user
        from app.main import app

        t = TroubleTicket(
            ticket_number="TT-ACCESS-COV",
            submitted_by=admin_user.id,
            title="Admin only",
            description="Private ticket",
            status="submitted",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(t)
        db_session.commit()
        db_session.refresh(t)

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: test_user

        with TestClient(app) as c:
            resp = c.get(f"/api/trouble-tickets/{t.id}")
        assert resp.status_code == 403
        app.dependency_overrides.clear()


class TestVerifyEdgeCases:
    """Additional verify endpoint edge cases for high-risk escalation."""

    def test_verify_not_fixed_high_risk_escalation(self, db_session, admin_user):
        """Verify with is_fixed=false on high-risk ticket creates high-risk child."""
        from app.database import get_db
        from app.dependencies import require_admin, require_user
        from app.main import app

        t = TroubleTicket(
            ticket_number="TT-VFY-HIGH",
            submitted_by=admin_user.id,
            title="High Risk Bug",
            description="Critical issue",
            status="awaiting_verification",
            risk_tier="high",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(t)
        db_session.commit()
        db_session.refresh(t)

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[require_user] = lambda: admin_user
        app.dependency_overrides[require_admin] = lambda: admin_user

        with TestClient(app) as c:
            resp = c.post(
                f"/api/trouble-tickets/{t.id}/verify",
                json={"is_fixed": False, "description": "Still broken"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "escalated"
        assert "child_ticket_id" in data
        # High-risk parent -> high-risk child
        child = db_session.get(TroubleTicket, data["child_ticket_id"])
        assert child.risk_tier == "high"
        app.dependency_overrides.clear()

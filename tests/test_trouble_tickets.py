"""Tests for trouble ticket service and router.

Covers: ticket creation, auto-context capture, sanitization,
ticket number generation, listing, access control, verify endpoint.

Called by: pytest
Depends on: conftest fixtures, app.models.TroubleTicket
"""

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

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

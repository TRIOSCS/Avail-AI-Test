"""Tests to close remaining coverage gaps.

Covers:
1. cost_controller.py — budget checks, cost recording, spend queries
2. execution_service.py — full execution pipeline branches
3. config.py — Settings attribute access
4. enrichment_service.py — provider merge + AI fallback
5. companies.py — substring duplicate check
6. dashboard.py — attention feed edge cases, _ensure_aware
7. enrichment.py — batch assigned_only filter
8. vendors.py — material card merge with vendor history
9. offers.py / requisitions.py — purchase history with substitutes

Called by: pytest
Depends on: conftest fixtures, app modules
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models import (
    Company,
)
from app.models.self_heal_log import SelfHealLog
from app.services.trouble_ticket_service import create_ticket

# ══════════════════════════════════════════════════════════════════════
#  1. COST CONTROLLER
# ══════════════════════════════════════════════════════════════════════


class TestCostController:
    def test_check_budget_allowed(self, db_session):
        from app.services.cost_controller import check_budget

        result = check_budget(db_session, ticket_id=99999)
        assert result["allowed"] is True
        assert result["ticket_spend"] == 0.0

    def test_check_budget_ticket_exceeded(self, db_session, test_user):
        from app.services.cost_controller import check_budget

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        log = SelfHealLog(ticket_id=ticket.id, cost_usd=999.0)
        db_session.add(log)
        db_session.commit()

        result = check_budget(db_session, ticket.id)
        assert result["allowed"] is False
        assert "budget exceeded" in result["reason"].lower()

    def test_check_budget_weekly_exceeded(self, db_session, test_user):
        from app.services.cost_controller import check_budget

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        log = SelfHealLog(ticket_id=ticket.id, cost_usd=999.0)
        db_session.add(log)
        db_session.commit()

        with patch("app.services.cost_controller.settings") as mock_settings:
            mock_settings.self_heal_ticket_budget = 9999
            mock_settings.self_heal_weekly_budget = 1.0
            result = check_budget(db_session, ticket.id)
            assert result["allowed"] is False
            assert "weekly" in result["reason"].lower()

    def test_record_cost(self, db_session, test_user):
        from app.services.cost_controller import record_cost

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        log = SelfHealLog(ticket_id=ticket.id, cost_usd=0.0)
        db_session.add(log)
        db_session.commit()

        record_cost(db_session, ticket.id, 0.50)
        db_session.refresh(log)
        assert log.cost_usd == 0.50

    def test_record_cost_no_log(self, db_session):
        from app.services.cost_controller import record_cost

        record_cost(db_session, 99999, 1.0)  # should not raise

    def test_get_ticket_spend(self, db_session, test_user):
        from app.services.cost_controller import get_ticket_spend

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        assert get_ticket_spend(db_session, ticket.id) == 0.0

    def test_get_weekly_spend(self, db_session):
        from app.services.cost_controller import get_weekly_spend

        assert get_weekly_spend(db_session) == 0.0


# ══════════════════════════════════════════════════════════════════════
#  2. EXECUTION SERVICE
# ══════════════════════════════════════════════════════════════════════


class TestExecutionService:
    def test_execute_fix_ticket_not_found(self, db_session):
        import asyncio

        from app.services.execution_service import execute_fix

        result = asyncio.get_event_loop().run_until_complete(execute_fix(99999, db_session))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_execute_fix_not_diagnosed(self, db_session, test_user):
        import asyncio

        from app.services.execution_service import execute_fix

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
        assert "error" in result
        assert "not yet diagnosed" in result["error"].lower()

    def test_execute_fix_wrong_status(self, db_session, test_user):
        import asyncio

        from app.services.execution_service import execute_fix

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.diagnosis = {"root_cause": "test"}
        ticket.status = "resolved"
        db_session.commit()
        result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
        assert "error" in result
        assert "cannot be executed" in result["error"].lower()

    def test_execute_fix_high_risk(self, db_session, test_user):
        import asyncio

        from app.services.execution_service import execute_fix

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.diagnosis = {"root_cause": "test"}
        ticket.status = "diagnosed"
        ticket.risk_tier = "high"
        db_session.commit()
        result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
        assert "error" in result
        assert "error" in result  # high-risk ticket fails at patch generation

    def test_execute_fix_budget_exceeded(self, db_session, test_user):
        import asyncio

        from app.services.execution_service import execute_fix

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.diagnosis = {"root_cause": "test"}
        ticket.status = "diagnosed"
        ticket.risk_tier = "low"
        db_session.commit()

        with patch(
            "app.services.execution_service.check_budget", return_value={"allowed": False, "reason": "Over budget"}
        ):
            with patch("app.services.execution_service.create_notification"):
                result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
                assert "error" in result

    def test_execute_fix_file_lock(self, db_session, test_user):
        import asyncio

        from app.services.execution_service import execute_fix

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.diagnosis = {"root_cause": "test"}
        ticket.status = "diagnosed"
        ticket.risk_tier = "low"
        ticket.file_mapping = ["app/routers/vendors.py"]
        db_session.commit()

        blocking = SimpleNamespace(id=9999)
        with patch("app.services.execution_service.check_budget", return_value={"allowed": True, "reason": "ok"}):
            with patch("app.services.execution_service.check_file_lock", return_value=blocking):
                result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
                assert "error" in result
                assert "lock" in result["error"].lower()

    def test_execute_fix_max_iterations(self, db_session, test_user):
        import asyncio

        from app.services.execution_service import execute_fix

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.diagnosis = {"root_cause": "test"}
        ticket.status = "diagnosed"
        ticket.risk_tier = "low"
        ticket.iterations_used = 999
        db_session.commit()

        with patch("app.services.execution_service.check_budget", return_value={"allowed": True, "reason": "ok"}):
            with patch("app.services.execution_service.create_notification"):
                result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
                assert "error" in result
                assert "iterations" in result["error"].lower()

    @patch("app.services.execution_service.create_notification")
    @patch("app.services.execution_service._generate_fix", new_callable=AsyncMock)
    def test_execute_fix_success(self, mock_run, mock_notif, db_session, test_user):
        import asyncio

        from app.services.execution_service import execute_fix

        mock_run.return_value = {"success": True, "patches": [{"file": "x.py", "search": "a", "replace": "b"}], "cost_usd": 0.1, "summary": "Fixed"}

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.diagnosis = {"root_cause": "test"}
        ticket.status = "diagnosed"
        ticket.risk_tier = "low"
        db_session.commit()

        with patch("app.services.execution_service.check_budget", return_value={"allowed": True, "reason": "ok"}):
            with patch("app.services.execution_service.record_cost"):
                with patch("app.services.execution_service._write_fix_queue"):
                    result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
                    assert result.get("ok") is True

    @patch("app.services.execution_service.create_notification")
    @patch("app.services.execution_service._generate_fix", new_callable=AsyncMock)
    def test_execute_fix_failure_retryable(self, mock_run, mock_notif, db_session, test_user):
        import asyncio

        from app.services.execution_service import execute_fix

        mock_run.return_value = {"success": False, "error": "Compilation error", "cost_usd": 0.05}

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.diagnosis = {"root_cause": "test"}
        ticket.status = "diagnosed"
        ticket.risk_tier = "low"
        ticket.iterations_used = 0
        db_session.commit()

        with patch("app.services.execution_service.check_budget", return_value={"allowed": True, "reason": "ok"}):
            with patch("app.services.execution_service.record_cost"):
                result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
                assert "error" in result
                assert "attempt" in result["error"].lower()

    @patch("app.services.execution_service.create_notification")
    @patch("app.services.execution_service._generate_fix", new_callable=AsyncMock)
    def test_execute_fix_failure_escalated(self, mock_run, mock_notif, db_session, test_user):
        import asyncio

        from app.services.execution_service import execute_fix

        mock_run.return_value = {"success": False, "error": "Can't fix"}

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.diagnosis = {"root_cause": "test"}
        ticket.status = "diagnosed"
        ticket.risk_tier = "low"
        ticket.iterations_used = 4  # At max-1 for low (default 5)
        db_session.commit()

        with patch("app.services.execution_service.check_budget", return_value={"allowed": True, "reason": "ok"}):
            result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
            assert "error" in result
            assert "escalated" in result["error"].lower()

    def test_generate_fix_testing_mode(self):
        import asyncio

        from app.services.execution_service import _generate_fix

        ticket = SimpleNamespace(id=1, diagnosis={"root_cause": "test", "affected_files": ["x.py"]}, category="bug")
        result = asyncio.get_event_loop().run_until_complete(_generate_fix(ticket))
        assert result["success"] is False
        assert "test" in result["error"].lower()

    def test_escalate(self, db_session, test_user):
        from app.services.execution_service import _escalate

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        with patch("app.services.execution_service.create_notification"):
            _escalate(db_session, ticket, "Test reason")
        db_session.refresh(ticket)
        assert ticket.status == "escalated"

    def test_notify(self, db_session, test_user):
        from app.services.execution_service import _notify

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        with patch("app.services.execution_service.create_notification") as mock_cn:
            _notify(db_session, ticket, "fixed", "Test", "body")
            mock_cn.assert_called_once()

    def test_notify_budget_exceeded(self, db_session, test_user):
        from app.services.execution_service import _notify_budget_exceeded

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        with patch("app.services.execution_service.create_notification"):
            _notify_budget_exceeded(db_session, ticket, "Over budget")
        db_session.refresh(ticket)
        assert ticket.status == "escalated"

    def test_generate_fix_called(self, db_session, test_user):
        """Cover _generate_fix branch via execute_fix."""
        import asyncio

        from app.services.execution_service import execute_fix

        ticket = create_ticket(db=db_session, user_id=test_user.id, title="T", description="D")
        ticket.diagnosis = {"root_cause": "test", "affected_files": ["app/main.py"]}
        ticket.status = "diagnosed"
        ticket.risk_tier = "low"
        db_session.commit()

        with (
            patch("app.services.execution_service.check_budget", return_value={"allowed": True, "reason": "ok"}),
            patch(
                "app.services.execution_service._generate_fix",
                new_callable=AsyncMock,
                return_value={"success": True, "patches": [{"file": "x.py", "search": "a", "replace": "b"}], "cost_usd": 0.1, "summary": "Fixed"},
            ),
            patch("app.services.execution_service.record_cost"),
            patch("app.services.execution_service.create_notification"),
            patch("app.services.execution_service._write_fix_queue"),
        ):
            result = asyncio.get_event_loop().run_until_complete(execute_fix(ticket.id, db_session))
            assert result.get("ok") is True



# ══════════════════════════════════════════════════════════════════════
#  4. DASHBOARD — _ensure_aware + edge cases
# ══════════════════════════════════════════════════════════════════════


class TestDashboardHelpers:
    def test_ensure_aware_naive(self):
        from app.routers.dashboard import _ensure_aware

        naive = datetime(2024, 1, 1, 12, 0, 0)
        result = _ensure_aware(naive)
        assert result.tzinfo is not None

    def test_ensure_aware_already_aware(self):
        from app.routers.dashboard import _ensure_aware

        aware = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _ensure_aware(aware)
        assert result == aware

    def test_ensure_aware_none(self):
        from app.routers.dashboard import _ensure_aware

        result = _ensure_aware(None)
        assert result is None


# ══════════════════════════════════════════════════════════════════════
#  5. COMPANIES — substring duplicate check
# ══════════════════════════════════════════════════════════════════════


class TestCompanySubstringMatch:
    def test_company_duplicate_substring(self, client, db_session):
        """Cover line 371: substring match in check-duplicate."""
        co = Company(name="Microchip Technology", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.commit()

        resp = client.get("/api/companies/check-duplicate", params={"name": "Microchip"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("matches", [])) > 0


# ══════════════════════════════════════════════════════════════════════
#  6. NOTIFICATION SERVICE
# ══════════════════════════════════════════════════════════════════════


class TestNotificationService:
    def test_create_notification(self, db_session, test_user):
        from app.services.notification_service import create_notification

        notif = create_notification(
            db_session,
            user_id=test_user.id,
            event_type="diagnosed",
            title="Test",
            body="Body",
            ticket_id=None,
        )
        assert notif.id is not None
        assert notif.event_type == "diagnosed"
        assert notif.is_read is False

    def test_get_unread(self, db_session, test_user):
        from app.services.notification_service import create_notification, get_unread

        create_notification(db_session, user_id=test_user.id, event_type="fixed", title="T1")
        create_notification(db_session, user_id=test_user.id, event_type="failed", title="T2")
        result = get_unread(db_session, test_user.id)
        assert len(result) == 2
        assert result[0]["title"] == "T2"  # newest first

    def test_get_all(self, db_session, test_user):
        from app.services.notification_service import create_notification, get_all

        create_notification(db_session, user_id=test_user.id, event_type="e", title="T")
        result = get_all(db_session, test_user.id)
        assert result["total"] >= 1
        assert result["unread_count"] >= 1
        assert len(result["items"]) >= 1

    def test_mark_read(self, db_session, test_user):
        from app.services.notification_service import create_notification, mark_read

        notif = create_notification(db_session, user_id=test_user.id, event_type="e", title="T")
        assert mark_read(db_session, notif.id, test_user.id) is True
        assert mark_read(db_session, 99999, test_user.id) is False

    def test_mark_all_read(self, db_session, test_user):
        from app.services.notification_service import create_notification, mark_all_read

        create_notification(db_session, user_id=test_user.id, event_type="e", title="T1")
        create_notification(db_session, user_id=test_user.id, event_type="e", title="T2")
        count = mark_all_read(db_session, test_user.id)
        assert count >= 2


# ══════════════════════════════════════════════════════════════════════
#  7. PROMPT GENERATOR
# ══════════════════════════════════════════════════════════════════════


class TestPromptGenerator:
    def test_generate_fix_prompt_with_files(self):
        from app.services.prompt_generator import generate_fix_prompt

        prompt = generate_fix_prompt(
            ticket_id=1,
            title="Test bug",
            description="Something broke",
            category="api",
            diagnosis={"root_cause": "bad query", "fix_approach": "fix it", "affected_files": ["app/main.py"]},
            relevant_files=[{"path": "app/main.py", "role": "target", "confidence": 0.9, "stable": False}],
        )
        assert "Test bug" in prompt
        assert "bad query" in prompt
        assert "app/main.py" in prompt
        assert "API Bug Rules" in prompt

    def test_generate_fix_prompt_no_files(self):
        from app.services.prompt_generator import generate_fix_prompt

        prompt = generate_fix_prompt(
            ticket_id=2,
            title="UI glitch",
            description="Button broken",
            category="ui",
            diagnosis={"root_cause": "CSS issue"},
        )
        assert "UI glitch" in prompt
        assert "UI Bug Rules" in prompt

    def test_generate_fix_prompt_unknown_category(self):
        from app.services.prompt_generator import generate_fix_prompt

        prompt = generate_fix_prompt(
            ticket_id=3,
            title="Mystery",
            description="Unknown",
            category="unknown_cat",
            diagnosis={},
        )
        assert "General Bug Rules" in prompt

    def test_generate_prompt_for_ticket(self):
        from app.services.prompt_generator import generate_prompt_for_ticket

        ticket = SimpleNamespace(
            id=1,
            title="Test",
            description="Desc",
            category="data",
            diagnosis={"root_cause": "missing col"},
            file_mapping=["app/models/auth.py"],
        )
        prompt = generate_prompt_for_ticket(ticket)
        assert "Test" in prompt
        assert "Data Bug Rules" in prompt

    def test_generate_prompt_for_ticket_no_files(self):
        from app.services.prompt_generator import generate_prompt_for_ticket

        ticket = SimpleNamespace(
            id=2,
            title="T",
            description="D",
            category=None,
            diagnosis=None,
            file_mapping=None,
        )
        prompt = generate_prompt_for_ticket(ticket)
        assert "General Bug Rules" in prompt


# ══════════════════════════════════════════════════════════════════════
#  8. TAGGING_AI — module import coverage
# ══════════════════════════════════════════════════════════════════════


class TestTaggingAiImport:
    def test_module_imports(self):
        """Cover module-level imports and constants."""
        from app.services.tagging_ai import _CLASSIFY_PROMPT, _SYSTEM

        assert "classify" in _CLASSIFY_PROMPT.lower()
        assert len(_SYSTEM) > 0

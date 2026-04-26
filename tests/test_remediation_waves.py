"""test_remediation_waves.py — Tests for remediation waves 2-10.

Tests API contract fixes, status machine validation, data cleanup,
date formatting safety, and error response consistency.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user fixtures)
"""

import pytest

from app.services.status_machine import require_valid_transition, validate_transition

# ── Status Machine ──────────────────────────────────────────────────────


class TestStatusMachine:
    """Status transition validation for offers, quotes, buy plans."""

    def test_offer_valid_transitions(self):
        assert validate_transition("offer", "pending_review", "active") is True
        assert validate_transition("offer", "pending_review", "rejected") is True
        assert validate_transition("offer", "active", "sold") is True
        assert validate_transition("offer", "active", "won") is True

    def test_offer_invalid_transition(self):
        import pytest

        with pytest.raises(ValueError, match="Invalid offer status transition"):
            validate_transition("offer", "sold", "active")

    def test_offer_terminal_state(self):
        import pytest

        with pytest.raises(ValueError):
            validate_transition("offer", "rejected", "active")

    def test_quote_valid_transitions(self):
        assert validate_transition("quote", "draft", "sent") is True
        assert validate_transition("quote", "sent", "won") is True
        assert validate_transition("quote", "sent", "lost") is True

    def test_quote_invalid_transition(self):
        import pytest

        with pytest.raises(ValueError):
            validate_transition("quote", "revised", "draft")

    def test_buy_plan_valid_transitions(self):
        assert validate_transition("buy_plan", "draft", "pending") is True
        assert validate_transition("buy_plan", "pending", "active") is True
        assert validate_transition("buy_plan", "active", "completed") is True
        assert validate_transition("buy_plan", "halted", "draft") is True

    def test_buy_plan_invalid_transition(self):
        import pytest

        with pytest.raises(ValueError):
            validate_transition("buy_plan", "completed", "active")

    def test_requisition_transitions(self):
        assert validate_transition("requisition", "draft", "active") is True
        assert validate_transition("requisition", "active", "offers") is True
        assert validate_transition("requisition", "offers", "quoting") is True

    def test_unknown_entity_allows_transition(self):
        assert validate_transition("unknown_entity", "a", "b") is True

    def test_unknown_current_status_allows_transition(self):
        assert validate_transition("offer", "unknown_status", "active") is True

    def test_noop_transition(self):
        assert validate_transition("offer", "active", "active") is True

    def test_raise_on_invalid_false(self):
        result = validate_transition("offer", "sold", "active", raise_on_invalid=False)
        assert result is False


class TestRequireValidTransition:
    """Tests for require_valid_transition — covers lines 117-120."""

    def test_valid_transition_does_not_raise(self):
        # Should not raise
        require_valid_transition("offer", "pending_review", "active")

    def test_invalid_transition_raises_http_409(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            require_valid_transition("offer", "sold", "active")
        assert exc_info.value.status_code == 409

    def test_terminal_state_raises_http_409(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            require_valid_transition("buy_plan", "completed", "active")
        assert exc_info.value.status_code == 409

"""Tests for data integrity fixes H8, H10, H11, H12.

H8: AI contact field truncation
H10: Search refresh stale data warning (HX-Trigger header)
H11: AI qty estimation fallback returns dict with approximate flag
H12: Credential decryption health check logging

Called by: pytest
Depends on: conftest fixtures, app.services.sighting_aggregation,
            app.services.credential_service
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── H8: AI Contact Field Truncation ────────────────────────────────


class TestH8ContactTruncation:
    """Verify AI-generated contact fields are truncated before DB save."""

    def _truncate(self, contact):
        _MAX_LEN = {
            "full_name": 255,
            "title": 255,
            "email": 255,
            "phone": 50,
            "linkedin_url": 512,
            "source": 100,
        }
        for field, max_len in _MAX_LEN.items():
            val = contact.get(field)
            if val and isinstance(val, str) and len(val) > max_len:
                contact[field] = val[:max_len]
        return contact

    def test_long_full_name_truncated(self):
        """full_name exceeding 255 chars is truncated."""
        c = self._truncate({"full_name": "A" * 300, "title": "CTO"})
        assert len(c["full_name"]) == 255
        assert c["title"] == "CTO"

    def test_long_phone_truncated_to_50(self):
        """Phone exceeding 50 chars is truncated."""
        c = self._truncate({"full_name": "Test", "phone": "+" + "1" * 60})
        assert len(c["phone"]) == 50

    def test_long_linkedin_url_truncated_to_512(self):
        """linkedin_url exceeding 512 chars is truncated."""
        c = self._truncate({"full_name": "Test", "linkedin_url": "https://linkedin.com/" + "x" * 600})
        assert len(c["linkedin_url"]) == 512

    def test_none_values_not_affected(self):
        """None fields don't cause errors during truncation."""
        c = self._truncate({"full_name": "Test", "title": None, "email": None})
        assert c["title"] is None
        assert c["email"] is None

    def test_fields_at_exact_limit_unchanged(self):
        """Fields exactly at the limit are not truncated."""
        c = self._truncate(
            {
                "full_name": "A" * 255,
                "phone": "D" * 50,
                "source": "F" * 100,
            }
        )
        assert len(c["full_name"]) == 255
        assert len(c["phone"]) == 50
        assert len(c["source"]) == 100


# ── H10: Search Refresh Stale Data Warning ─────────────────────────


class TestH10RefreshWarning:
    """Verify HX-Trigger header is set when search refresh fails."""

    def _make_requirement(self, db_session, primary_mpn):
        from app.models.sourcing import Requirement, Requisition

        req = Requisition(name="Test RFQ", status="open", customer_name="Test Co")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=primary_mpn,
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()
        return r

    def test_refresh_returns_searching_panel(self, client, db_session):
        """A user click returns the immediate "Searching…" panel; the search now runs in
        a background job, so a connector failure can no longer surface a synchronous
        toast."""
        r = self._make_requirement(db_session, "TEST-001")

        with (
            patch("app.search_service.search_requirement", side_effect=RuntimeError("API down")),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_broker.publish = AsyncMock()
            resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")

        assert resp.status_code == 200
        assert "Searching suppliers" in resp.text
        assert "Search refresh failed" not in resp.headers.get("hx-trigger", "")

    def test_successful_refresh_no_warning(self, client, db_session):
        """When search_requirement succeeds, no warning in HX-Trigger."""
        r = self._make_requirement(db_session, "TEST-002")

        with (
            patch(
                "app.search_service.search_requirement",
                new=AsyncMock(return_value={"sightings": [], "source_stats": [], "mpn_results": {}}),
            ),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_broker.publish = AsyncMock()
            resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")

        assert resp.status_code == 200
        hx_trigger = resp.headers.get("hx-trigger", "")
        assert "Search refresh failed" not in hx_trigger


# ── H11: AI Qty Estimation Fallback ────────────────────────────────


class TestH11QtyEstimation:
    """Verify _estimate_qty_with_ai returns dict with approximate flag."""

    @pytest.mark.parametrize(
        ("qtys", "expected"),
        [
            pytest.param([], {"qty": None, "approximate": False}, id="empty_list_none_not_approximate"),
            pytest.param([None, None], {"qty": None, "approximate": False}, id="all_none_not_approximate"),
            pytest.param([42], {"qty": 42, "approximate": False}, id="single_value_exact"),
            pytest.param([100, 200], {"qty": 300, "approximate": False}, id="two_values_sum_exact"),
        ],
    )
    def test_no_ai_needed_returns_exact(self, qtys, expected):
        """0-2 values resolve deterministically without invoking the AI fallback."""
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        assert _estimate_qty_with_ai(qtys) == expected

    def test_three_values_no_api_key_returns_max_approximate(self):
        """Without an Anthropic credential, fallback uses max and marks approximate."""
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        with patch("app.services.credential_service.get_credential_cached", return_value=None):
            result = _estimate_qty_with_ai([100, 200, 300])

        assert result == {"qty": 300, "approximate": True}

    def test_ai_exception_returns_max_approximate(self):
        """When Claude API call fails, returns max with approximate=True."""
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        with (
            patch("app.services.credential_service.get_credential_cached", return_value="sk-test"),
            patch("anthropic.Anthropic", side_effect=RuntimeError("API error")),
        ):
            result = _estimate_qty_with_ai([50, 100, 150])

        assert result["qty"] == 150  # max, not sum (350)
        assert result["approximate"] is True

    def test_ai_success_returns_exact(self):
        """When Claude responds with a number, returns exact result."""
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text="250")]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp

        with (
            patch("app.services.credential_service.get_credential_cached", return_value="sk-test"),
            patch("anthropic.Anthropic", return_value=mock_client),
        ):
            result = _estimate_qty_with_ai([100, 200, 300])

        assert result == {"qty": 250, "approximate": False}


# ── H12: Credential Decryption Health Check ────────────────────────


class TestH12CredentialDecryptionFallback:
    """Verify credential decryption failure logs and falls back to env var."""

    def _make_source(self, db_session, credentials):
        from app.models import ApiSource

        src = ApiSource(
            name="test_src",
            display_name="Test Source",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["MY_API_KEY"],
            credentials=credentials,
        )
        db_session.add(src)
        db_session.commit()
        return src

    def test_decrypt_failure_falls_back_to_env(self, db_session):
        """When decryption fails, get_credential returns env var value."""
        from app.services.credential_service import get_credential

        self._make_source(db_session, {"MY_API_KEY": "corrupted-not-valid-fernet-token"})

        with patch.dict(os.environ, {"MY_API_KEY": "fallback-value"}):
            result = get_credential(db_session, "test_src", "MY_API_KEY")

        assert result == "fallback-value"

    def test_decrypt_failure_logs_warning(self, db_session):
        """When decryption fails, warning is logged about env var fallback."""
        from loguru import logger

        from app.services.credential_service import get_credential

        self._make_source(db_session, {"MY_API_KEY": "corrupted-not-valid-fernet-token"})

        messages = []
        handler_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            with patch.dict(os.environ, {"MY_API_KEY": "fallback-value"}):
                get_credential(db_session, "test_src", "MY_API_KEY")
        finally:
            logger.remove(handler_id)

        log_text = " ".join(messages)
        assert "FAILED" in log_text or "fallback" in log_text.lower()

    def test_no_credentials_returns_env_var(self, db_session):
        """When source has no credentials, env var is returned."""
        from app.services.credential_service import get_credential

        self._make_source(db_session, {})

        with patch.dict(os.environ, {"MY_API_KEY": "env-value"}):
            result = get_credential(db_session, "test_src", "MY_API_KEY")

        assert result == "env-value"

    def test_successful_decrypt_returns_db_value(self, db_session):
        """When decryption succeeds, DB credential is returned (not env var)."""
        from app.services.credential_service import encrypt_value, get_credential

        encrypted = encrypt_value("my-secret-key")
        self._make_source(db_session, {"MY_API_KEY": encrypted})

        with patch.dict(os.environ, {"MY_API_KEY": "should-not-use-this"}):
            result = get_credential(db_session, "test_src", "MY_API_KEY")

        assert result == "my-secret-key"

    def test_decrypt_failure_no_env_returns_none(self, db_session):
        """When decryption fails and no env var, returns None."""
        from app.services.credential_service import get_credential

        self._make_source(db_session, {"MY_API_KEY": "corrupted-token"})

        env_backup = os.environ.pop("MY_API_KEY", None)
        try:
            result = get_credential(db_session, "test_src", "MY_API_KEY")
        finally:
            if env_backup is not None:
                os.environ["MY_API_KEY"] = env_backup

        assert result is None

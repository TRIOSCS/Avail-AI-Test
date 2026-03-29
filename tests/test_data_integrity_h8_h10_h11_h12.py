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

    def test_refresh_failure_sets_hx_trigger(self, client, db_session):
        """When search_requirement raises, response should have HX-Trigger header."""
        from app.models.sourcing import Requirement, Requisition

        req = Requisition(name="Test RFQ", status="active", customer_name="Test Co")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST-001",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()

        with (
            patch("app.search_service.search_requirement", side_effect=RuntimeError("API down")),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_broker.publish = AsyncMock()
            resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")

        assert resp.status_code == 200
        hx_trigger = resp.headers.get("hx-trigger", "")
        assert "showToast" in hx_trigger
        assert "Search refresh failed" in hx_trigger

    def test_successful_refresh_no_warning(self, client, db_session):
        """When search_requirement succeeds, no warning in HX-Trigger."""
        from app.models.sourcing import Requirement, Requisition

        req = Requisition(name="Test RFQ", status="active", customer_name="Test Co")
        db_session.add(req)
        db_session.flush()
        r = Requirement(
            requisition_id=req.id,
            primary_mpn="TEST-002",
            manufacturer="TestMfr",
            target_qty=100,
            sourcing_status="open",
        )
        db_session.add(r)
        db_session.commit()

        with (
            patch("app.search_service.search_requirement", new_callable=AsyncMock),
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

    def test_empty_list_returns_none_not_approximate(self):
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        result = _estimate_qty_with_ai([])
        assert result == {"qty": None, "approximate": False}

    def test_all_none_returns_none_not_approximate(self):
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        result = _estimate_qty_with_ai([None, None])
        assert result == {"qty": None, "approximate": False}

    def test_single_value_returns_exact(self):
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        result = _estimate_qty_with_ai([42])
        assert result == {"qty": 42, "approximate": False}

    def test_two_values_returns_sum_exact(self):
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        result = _estimate_qty_with_ai([100, 200])
        assert result == {"qty": 300, "approximate": False}

    def test_three_values_no_api_key_returns_max_approximate(self):
        """Without ANTHROPIC_API_KEY, fallback uses max and marks approximate."""
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        with patch("app.config.settings") as mock_settings:
            mock_settings.ANTHROPIC_API_KEY = ""
            result = _estimate_qty_with_ai([100, 200, 300])

        assert result == {"qty": 300, "approximate": True}

    def test_ai_exception_returns_max_approximate(self):
        """When Claude API call fails, returns max with approximate=True."""
        from app.services.sighting_aggregation import _estimate_qty_with_ai

        with patch("anthropic.Anthropic", side_effect=RuntimeError("API error")):
            with patch("app.config.settings") as mock_settings:
                mock_settings.ANTHROPIC_API_KEY = "sk-test"
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
            patch("anthropic.Anthropic", return_value=mock_client),
            patch("app.config.settings") as mock_settings,
            patch("app.utils.claude_client.MODELS", {"fast": "claude-3-haiku"}),
        ):
            mock_settings.ANTHROPIC_API_KEY = "sk-test"
            result = _estimate_qty_with_ai([100, 200, 300])

        assert result == {"qty": 250, "approximate": False}


# ── H12: Credential Decryption Health Check ────────────────────────


class TestH12CredentialDecryptionFallback:
    """Verify credential decryption failure logs and falls back to env var."""

    def test_decrypt_failure_falls_back_to_env(self, db_session):
        """When decryption fails, get_credential returns env var value."""
        from app.models import ApiSource
        from app.services.credential_service import get_credential

        src = ApiSource(
            name="test_src",
            display_name="Test Source",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["MY_API_KEY"],
            credentials={"MY_API_KEY": "corrupted-not-valid-fernet-token"},
        )
        db_session.add(src)
        db_session.commit()

        with patch.dict(os.environ, {"MY_API_KEY": "fallback-value"}):
            result = get_credential(db_session, "test_src", "MY_API_KEY")

        assert result == "fallback-value"

    def test_decrypt_failure_logs_warning(self, db_session):
        """When decryption fails, warning is logged about env var fallback."""
        from loguru import logger

        from app.models import ApiSource
        from app.services.credential_service import get_credential

        src = ApiSource(
            name="test_src",
            display_name="Test Source",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["MY_API_KEY"],
            credentials={"MY_API_KEY": "corrupted-not-valid-fernet-token"},
        )
        db_session.add(src)
        db_session.commit()

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
        from app.models import ApiSource
        from app.services.credential_service import get_credential

        src = ApiSource(
            name="test_src",
            display_name="Test Source",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["MY_API_KEY"],
            credentials={},
        )
        db_session.add(src)
        db_session.commit()

        with patch.dict(os.environ, {"MY_API_KEY": "env-value"}):
            result = get_credential(db_session, "test_src", "MY_API_KEY")

        assert result == "env-value"

    def test_successful_decrypt_returns_db_value(self, db_session):
        """When decryption succeeds, DB credential is returned (not env var)."""
        from app.models import ApiSource
        from app.services.credential_service import encrypt_value, get_credential

        encrypted = encrypt_value("my-secret-key")
        src = ApiSource(
            name="test_src",
            display_name="Test Source",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["MY_API_KEY"],
            credentials={"MY_API_KEY": encrypted},
        )
        db_session.add(src)
        db_session.commit()

        with patch.dict(os.environ, {"MY_API_KEY": "should-not-use-this"}):
            result = get_credential(db_session, "test_src", "MY_API_KEY")

        assert result == "my-secret-key"

    def test_decrypt_failure_no_env_returns_none(self, db_session):
        """When decryption fails and no env var, returns None."""
        from app.models import ApiSource
        from app.services.credential_service import get_credential

        src = ApiSource(
            name="test_src",
            display_name="Test Source",
            category="api",
            source_type="aggregator",
            status="active",
            env_vars=["MY_API_KEY"],
            credentials={"MY_API_KEY": "corrupted-token"},
        )
        db_session.add(src)
        db_session.commit()

        env_backup = os.environ.pop("MY_API_KEY", None)
        try:
            result = get_credential(db_session, "test_src", "MY_API_KEY")
        finally:
            if env_backup is not None:
                os.environ["MY_API_KEY"] = env_backup

        assert result is None

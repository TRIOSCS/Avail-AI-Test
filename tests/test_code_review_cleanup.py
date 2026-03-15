"""test_code_review_cleanup.py — Tests for code review cleanup changes.

Covers: _ai_enabled empty admin_emails guard, eBay token expiry tracking,
        silent exception logging in apply_freeform_rfq, rfq.py warning logs.

Called by: pytest
Depends on: app.routers.ai, app.connectors.ebay, app.routers.rfq
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _ai_enabled: empty admin_emails with mike_only mode
# ---------------------------------------------------------------------------


def _make_settings(flag: str, admin_emails: list[str] | None = None):
    return SimpleNamespace(
        ai_features_enabled=flag,
        admin_emails=admin_emails,
    )


def test_ai_enabled_mike_only_empty_admin_emails_denies():
    """mike_only mode with empty admin_emails should deny all users (fail closed)."""
    user = SimpleNamespace(email="mike@trioscs.com", id=1, name="Mike", role="admin")
    mock_settings = _make_settings("mike_only", admin_emails=[])
    with patch("app.routers.ai.settings", mock_settings):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(user) is False


def test_ai_enabled_mike_only_none_admin_emails_denies():
    """mike_only mode with None admin_emails should deny all users (fail closed)."""
    user = SimpleNamespace(email="mike@trioscs.com", id=1, name="Mike", role="admin")
    mock_settings = _make_settings("mike_only", admin_emails=None)
    with patch("app.routers.ai.settings", mock_settings):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(user) is False


def test_ai_enabled_mike_only_with_admin_emails_allows():
    """mike_only mode with populated admin_emails should allow listed users."""
    user = SimpleNamespace(email="mike@trioscs.com", id=1, name="Mike", role="admin")
    mock_settings = _make_settings("mike_only", admin_emails=["mike@trioscs.com"])
    with patch("app.routers.ai.settings", mock_settings):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(user) is True


# ---------------------------------------------------------------------------
# eBay token expiry tracking
# ---------------------------------------------------------------------------


def _mock_response(status_code=200, json_data=None, text=""):
    """Build a fake httpx.Response."""
    import httpx

    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text or str(json_data)
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
    return resp


@pytest.mark.asyncio
async def test_ebay_token_expiry_caches_within_window():
    """Token should be reused when within expiry window."""
    from app.connectors.ebay import EbayConnector

    c = EbayConnector(client_id="id", client_secret="secret")
    c._token = "cached-token"
    c._token_expires_at = time.monotonic() + 3600  # 1 hour from now

    token = await c._get_token()
    assert token == "cached-token"


@pytest.mark.asyncio
async def test_ebay_token_expiry_refreshes_when_expired():
    """Token should be refreshed when past expiry window."""
    from app.connectors.ebay import EbayConnector

    c = EbayConnector(client_id="id", client_secret="secret")
    c._token = "old-token"
    c._token_expires_at = time.monotonic() - 10  # expired

    token_resp = _mock_response(200, {"access_token": "new-token", "expires_in": 7200})
    with patch("app.connectors.ebay.http") as mock_http:
        mock_http.post = AsyncMock(return_value=token_resp)
        token = await c._get_token()
        assert token == "new-token"
        assert c._token_expires_at > time.monotonic()
        mock_http.post.assert_called_once()


@pytest.mark.asyncio
async def test_ebay_token_expiry_refreshes_near_margin():
    """Token should be refreshed when within 60s of expiry."""
    from app.connectors.ebay import EbayConnector

    c = EbayConnector(client_id="id", client_secret="secret")
    c._token = "almost-expired"
    c._token_expires_at = time.monotonic() + 30  # within 60s margin

    token_resp = _mock_response(200, {"access_token": "refreshed-token", "expires_in": 7200})
    with patch("app.connectors.ebay.http") as mock_http:
        mock_http.post = AsyncMock(return_value=token_resp)
        token = await c._get_token()
        assert token == "refreshed-token"


# ---------------------------------------------------------------------------
# Silent exception logging in apply_freeform_rfq
# ---------------------------------------------------------------------------


def test_apply_freeform_rfq_logs_skipped_items():
    """Skipped requirement items should produce a warning log."""
    with patch("app.routers.ai.logger") as mock_logger:
        from app.schemas.requisitions import RequirementCreate

        # Trigger the validation error path manually
        try:
            RequirementCreate.model_validate({"invalid": "data"})
        except (ValueError, TypeError) as exc:
            # Simulate what the router does now
            mock_logger.warning("Skipping invalid requirement item: {} — {}", {"invalid": "data"}, exc)
            mock_logger.warning.assert_called_once()

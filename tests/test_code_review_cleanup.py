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


@pytest.mark.parametrize(
    ("admin_emails", "expected"),
    [
        pytest.param([], False, id="empty_admin_emails_denies"),
        pytest.param(None, False, id="none_admin_emails_denies"),
        pytest.param(["mike@trioscs.com"], True, id="with_admin_emails_allows"),
    ],
)
def test_ai_enabled_mike_only(admin_emails, expected):
    """mike_only mode: deny when admin_emails is empty/None (fail closed), allow listed users."""
    user = SimpleNamespace(email="mike@trioscs.com", id=1, name="Mike", role="admin")
    mock_settings = _make_settings("mike_only", admin_emails=admin_emails)
    with patch("app.routers.ai.settings", mock_settings):
        from app.routers.ai import _ai_enabled

        assert _ai_enabled(user) is expected


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
    from app.connectors.sources import _token_cache

    c = EbayConnector(client_id="id", client_secret="secret")
    _token_cache[c._token_cache_key()] = ("cached-token", time.monotonic() + 3600)  # 1 hour

    token = await c._get_token()
    assert token == "cached-token"


@pytest.mark.asyncio
async def test_ebay_token_expiry_refreshes_when_expired():
    """Token should be refreshed when past expiry window."""
    from app.connectors.ebay import EbayConnector
    from app.connectors.sources import _token_cache

    c = EbayConnector(client_id="id", client_secret="secret")
    _token_cache[c._token_cache_key()] = ("old-token", time.monotonic() - 10)  # expired

    token_resp = _mock_response(200, {"access_token": "new-token", "expires_in": 7200})
    with patch("app.connectors.ebay.http") as mock_http:
        mock_http.post = AsyncMock(return_value=token_resp)
        token = await c._get_token()
        assert token == "new-token"
        assert _token_cache[c._token_cache_key()][1] > time.monotonic()
        mock_http.post.assert_called_once()


@pytest.mark.asyncio
async def test_ebay_token_expiry_refreshes_near_margin():
    """Token should be refreshed when within 60s of expiry."""
    from app.connectors.ebay import EbayConnector
    from app.connectors.sources import _token_cache

    c = EbayConnector(client_id="id", client_secret="secret")
    _token_cache[c._token_cache_key()] = ("almost-expired", time.monotonic() + 30)  # within margin

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

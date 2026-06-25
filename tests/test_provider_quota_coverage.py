"""tests/test_provider_quota_coverage.py — Verify config flags + quota-circuit coverage
for the hunter connector.

Called by: pytest
Depends on: app.config.settings, app.connectors.hunter,
            app.services.enrichment_credit_guard.ProviderQuotaError
"""

import os

os.environ.setdefault("TESTING", "1")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.enrichment_credit_guard import ProviderQuotaError


def test_new_provider_settings_exist():
    for attr in (
        "hunter_enrichment_enabled",
        "hunter_cooldown_minutes",
        "sam_gov_enrichment_enabled",
    ):
        assert hasattr(settings, attr), f"settings missing attribute: {attr}"


@pytest.mark.asyncio
async def test_hunter_raises_quota_on_429():
    from app.connectors.hunter import HunterConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    with patch("app.connectors.hunter.http") as mock_http:
        mock_http.get = AsyncMock(return_value=mock_resp)
        with pytest.raises(ProviderQuotaError):
            await HunterConnector("key").domain_search("example.com")


@pytest.mark.asyncio
async def test_hunter_raises_quota_on_402():
    from app.connectors.hunter import HunterConnector

    mock_resp = MagicMock()
    mock_resp.status_code = 402
    with patch("app.connectors.hunter.http") as mock_http:
        mock_http.get = AsyncMock(return_value=mock_resp)
        with pytest.raises(ProviderQuotaError):
            await HunterConnector("key").domain_search("example.com")

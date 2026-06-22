"""tests/test_provider_quota_coverage.py — Verify config flags + quota-circuit coverage
for apollo and hunter connectors.

Called by: pytest
Depends on: app.config.settings, app.connectors.apollo, app.connectors.hunter,
            app.services.enrichment_credit_guard.ProviderQuotaError
"""

import os

os.environ.setdefault("TESTING", "1")

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.services.enrichment_credit_guard import ProviderQuotaError


def test_new_provider_settings_exist():
    for attr in (
        "apollo_enrichment_enabled",
        "apollo_cooldown_minutes",
        "hunter_enrichment_enabled",
        "hunter_cooldown_minutes",
        "sam_gov_enrichment_enabled",
    ):
        assert hasattr(settings, attr), f"settings missing attribute: {attr}"


@contextmanager
def _patch_apollo_client(status_code: int):
    """Patch httpx.AsyncClient used by apollo so it returns a response with the given
    status_code."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        yield


@pytest.mark.asyncio
async def test_apollo_raises_quota_on_429():
    from app.connectors import apollo

    with _patch_apollo_client(429):
        with pytest.raises(ProviderQuotaError):
            await apollo.search_company("x.com", "key")


@pytest.mark.asyncio
async def test_apollo_raises_quota_on_402():
    from app.connectors import apollo

    with _patch_apollo_client(402):
        with pytest.raises(ProviderQuotaError):
            await apollo.search_company("x.com", "key")


@pytest.mark.asyncio
async def test_apollo_search_contacts_raises_quota_on_429():
    from app.connectors import apollo

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(ProviderQuotaError):
            await apollo.search_contacts("x.com", "key")


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


@pytest.mark.asyncio
async def test_apollo_search_contacts_raises_quota_on_402():
    from app.connectors import apollo

    mock_resp = MagicMock()
    mock_resp.status_code = 402
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    with patch("app.connectors.apollo.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        with pytest.raises(ProviderQuotaError):
            await apollo.search_contacts("x.com", "key")

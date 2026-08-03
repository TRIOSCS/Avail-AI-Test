import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.connectors.errors import ConnectorQuotaError, ConnectorRateLimitError
from app.services.authoritative_enrichment_service import fetch_authoritative


def _mock_response(status_code=200, json_data=None, text=""):
    """Build a fake httpx.Response (same shape as test_connector_rate_limits)."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text or str(json_data)
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
    return resp


def _rl_conn(name="element14"):
    class _C:
        source_name = name
        calls = 0

        async def search(self, pn):
            type(self).calls += 1
            raise ConnectorRateLimitError("element14 rate limited (QPS)")

    return _C()


def test_rate_limit_cools_down_not_disabled():
    conn = _rl_conn()
    disabled: set[str] = set()
    cooldown: dict[str, float] = {}
    # First MPN: rate-limited -> cooldown set, NOT permanently disabled
    asyncio.run(fetch_authoritative("A", "a", [conn], disabled, cooldown))
    assert "element14" not in disabled
    assert "element14" in cooldown
    calls_after_first = type(conn).calls
    # Second MPN immediately: still in cooldown -> skipped (no new call)
    asyncio.run(fetch_authoritative("B", "b", [conn], disabled, cooldown))
    assert type(conn).calls == calls_after_first


def test_quota_still_disables():
    class _Q:
        source_name = "oemsecrets"

        async def search(self, pn):
            raise ConnectorQuotaError("out of api calls")

    disabled: set[str] = set()
    asyncio.run(fetch_authoritative("A", "a", [_Q()], disabled, {}))
    assert "oemsecrets" in disabled


@pytest.mark.asyncio
async def test_mouser_403_rate_limit_recovers_within_run(monkeypatch):
    """A mouser-style HTTP 403 'Maximum calls per minute exceeded' (real connector,
    mocked HTTP) must land in ``cooldown`` — NOT ``disabled`` — and the connector must
    be queried again once the cooldown expires, all within the same run."""
    import app.services.authoritative_enrichment_service as aes
    from app.connectors.mouser import MouserConnector

    monkeypatch.setattr(aes, "_RATE_COOLDOWN_SECONDS", 0.05)
    conn = MouserConnector(api_key="test-key")
    conn._breaker.record_success()  # breakers are class-global — reset from prior tests

    rate_403 = _mock_response(403, text="Maximum calls per minute exceeded")
    ok = _mock_response(
        200,
        json_data={"SearchResults": {"Parts": [{"ManufacturerPartNumber": "LM317T", "Description": "1.5A regulator"}]}},
    )

    with patch("app.connectors.mouser.http") as mock_http:
        mock_http.post = AsyncMock(side_effect=[rate_403, ok])
        disabled: set[str] = set()
        cooldown: dict[str, float] = {}

        await fetch_authoritative("LM317T", "lm317t", [conn], disabled, cooldown)
        assert "mouser" not in disabled, "per-minute 403 must not disable mouser for the run"
        assert "mouser" in cooldown

        # Still cooling down: skipped, no upstream call burned.
        await fetch_authoritative("LM317T", "lm317t", [conn], disabled, cooldown)
        assert mock_http.post.call_count == 1

        # Cooldown expired: re-enabled within the same run and contributing again.
        await asyncio.sleep(0.06)
        results = await fetch_authoritative("LM317T", "lm317t", [conn], disabled, cooldown)
        assert mock_http.post.call_count == 2
        assert results["mouser"][0]["mpn_matched"] == "LM317T"


@pytest.mark.asyncio
async def test_oemsecrets_401_still_disables_for_run():
    """A true auth/quota failure (oemsecrets HTTP 401 'User is not accepted or has run
    out of api calls', real connector, mocked HTTP) must still disable the source for
    the rest of the run."""
    from app.connectors.oemsecrets import OEMSecretsConnector

    conn = OEMSecretsConnector(api_key="test-key")
    conn._breaker.record_success()  # breakers are class-global — reset from prior tests

    resp_401 = _mock_response(401, text="User is not accepted or has run out of api calls")

    with patch("app.connectors.oemsecrets.http") as mock_http:
        mock_http.get = AsyncMock(return_value=resp_401)
        disabled: set[str] = set()
        cooldown: dict[str, float] = {}

        await fetch_authoritative("LM358N", "lm358n", [conn], disabled, cooldown)
        assert "oemsecrets" in disabled

        # Disabled for the run: skipped, no further upstream calls.
        await fetch_authoritative("LM358N", "lm358n", [conn], disabled, cooldown)
        assert mock_http.get.call_count == 1

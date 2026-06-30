"""Regression tests: connector API keys must never leak to the logs.

Connectors that authenticate with the API key as a URL query param (Mouser,
element14, OEMSecrets, Nexar REST) raise httpx.HTTPStatusError on an unhandled
status, whose str() embeds the FULL request URL including ``?apiKey=SECRET``.
BaseConnector logs that string at WARNING to every loguru sink. These tests pin
that the secret is scrubbed (REDACTED) before it reaches the log sink.

Covers: app/connectors/sources.py _redact_secrets helper + the two
BaseConnector._search_with_retry WARNING log paths.

All external HTTP is faked — no real API requests.
"""

import httpx
import pytest
from loguru import logger

from app.connectors.sources import BaseConnector, _redact_secrets

SECRET = "SUPERSECRETKEY1234567890"


# ═══════════════════════════════════════════════════════════════════════
#  _redact_secrets — unit
# ═══════════════════════════════════════════════════════════════════════


class TestRedactSecrets:
    @pytest.mark.parametrize(
        "raw",
        [
            f"https://api.mouser.com/api/v2/search/keyword?apiKey={SECRET}",
            f"https://api.element14.com/catalog/products?term=ABC&callInfo.apiKey={SECRET}",
            f"https://oemsecretsapi.com/partsearch?apiKey={SECRET}&searchTerm=ABC",
            f"https://octopart.com/api/v4/rest/parts/search?q=ABC&apikey={SECRET}&limit=20",
            f"https://x.test/p?api_key={SECRET}",
            f"https://x.test/p?key={SECRET}",
            f"https://x.test/p?token={SECRET}&q=1",
            f"https://x.test/p?APIKEY={SECRET}",  # case-insensitive
        ],
    )
    def test_secret_value_is_redacted(self, raw):
        out = _redact_secrets(raw)
        assert SECRET not in out
        assert "REDACTED" in out

    def test_non_secret_params_preserved(self):
        raw = f"https://x.test/p?q=ABC123&apiKey={SECRET}&limit=20"
        out = _redact_secrets(raw)
        assert "q=ABC123" in out
        assert "limit=20" in out
        assert SECRET not in out

    def test_does_not_overmatch_unrelated_param(self):
        # A param that merely contains "key" as a substring is not a secret.
        raw = "https://x.test/p?monkey=banana&donkey=kong"
        assert _redact_secrets(raw) == raw

    def test_redacts_full_httpx_error_message(self):
        request = httpx.Request("GET", f"https://api.example.com/s?apiKey={SECRET}&q=ABC")
        response = httpx.Response(500, request=request)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            msg = str(exc)
        assert SECRET in msg  # sanity: httpx really does embed the URL
        out = _redact_secrets(msg)
        assert SECRET not in out
        assert "REDACTED" in out


# ═══════════════════════════════════════════════════════════════════════
#  BaseConnector log path — integration
# ═══════════════════════════════════════════════════════════════════════


class _LeakyConnector(BaseConnector):
    """A connector whose _do_search raises an HTTPStatusError for a URL whose query
    string carries the API key — exactly how a 5xx from Mouser/element14/ OEMSecrets
    surfaces through BaseConnector."""

    source_name = "leaky"

    def __init__(self):
        # max_retries=0 → the single attempt fails and logs immediately.
        super().__init__(max_retries=0)

    async def _do_search(self, part_number: str) -> list[dict]:
        request = httpx.Request("GET", f"https://api.example.com/search?apiKey={SECRET}&q={part_number}")
        response = httpx.Response(500, request=request)
        response.raise_for_status()  # raises httpx.HTTPStatusError with the URL in str()
        return []


@pytest.mark.asyncio
async def test_search_with_retry_does_not_log_api_key():
    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING", format="{message}")
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await _LeakyConnector()._search_with_retry("ABC123")
    finally:
        logger.remove(sink_id)

    blob = "".join(captured)
    assert blob, "expected a WARNING log line for the exhausted HTTP error"
    assert SECRET not in blob, f"API key leaked to logs: {blob!r}"
    assert "REDACTED" in blob

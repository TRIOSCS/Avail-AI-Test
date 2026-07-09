"""Regression: the exception PROPAGATED out of BaseConnector._search_with_retry must
not carry a secret URL query param.

search_service logs str(e) + the traceback, persists str(e) to the source-stats row,
and streams it in the SSE error field — so redacting only the inner BaseConnector log
line (the first pass) was insufficient. A 401 (wrong key) or an exhausted 5xx must
surface a redacted exception.

Called by: pytest
Depends on: app.connectors.sources
"""

import httpx

from app.connectors.sources import BaseConnector

_SECRET_URL = "https://api.mouser.com/api/v1/search?apiKey=SUPER_SECRET_KEY_123"


class _LeakyConnector(BaseConnector):
    source_name = "leaky"

    def __init__(self, status: int):
        super().__init__(max_retries=0)
        self._status = status

    async def _do_search(self, part_number: str):
        req = httpx.Request("GET", _SECRET_URL)
        resp = httpx.Response(self._status, request=req)
        raise httpx.HTTPStatusError(f"Client error '{self._status}' for url '{req.url}'", request=req, response=resp)


async def test_auth_401_reraise_is_redacted():
    conn = _LeakyConnector(401)
    raised = None
    try:
        await conn._search_with_retry("LM317")
    except Exception as e:
        raised = e
    assert raised is not None
    assert "SUPER_SECRET_KEY_123" not in str(raised)
    assert "REDACTED" in str(raised)


async def test_exhausted_5xx_reraise_is_redacted():
    conn = _LeakyConnector(500)
    raised = None
    try:
        await conn._search_with_retry("LM317")
    except Exception as e:
        raised = e
    assert raised is not None
    assert "SUPER_SECRET_KEY_123" not in str(raised)
    assert "REDACTED" in str(raised)

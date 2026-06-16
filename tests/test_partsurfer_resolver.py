"""tests/test_partsurfer_resolver.py -- fetch_partsurfer_description unit tests.

Covers: app/services/enrichment_worker/partsurfer_resolver.py. Patches the shared
``app.http_client.http_redirect.get`` with an AsyncMock so ZERO live network is touched —
the two real fixtures (a DIMM + an SSD captured from partsurfer.hpe.com) drive the happy
path; the fabricated not-found fixture, a non-200 status, and a raised httpx.HTTPError
drive the resilience contract (every failure → None, never a raise into the worker).
Depends on: tests/fixtures/partsurfer/*.html.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.enrichment_worker import partsurfer_resolver

_FIXTURES = Path(__file__).parent / "fixtures" / "partsurfer"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _resp(text: str, status_code: int = 200):
    """A minimal stand-in for httpx.Response: only .status_code + .text are read."""
    resp = type("Resp", (), {})()
    resp.status_code = status_code
    resp.text = text
    return resp


@pytest.mark.asyncio
async def test_dimm_fixture_returns_exact_description():
    resp = _resp(_fixture("dimm_726719-B21.html"))
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        desc = await partsurfer_resolver.fetch_partsurfer_description("726719-B21")
    assert desc == "HPE 16GB (1X16GB) DUAL RANK X4 DDR4-2133 CAS-15-15-15 REGISTERED MEMORY KIT"


@pytest.mark.asyncio
async def test_ssd_fixture_returns_exact_description():
    resp = _resp(_fixture("ssd_875507-B21.html"))
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        desc = await partsurfer_resolver.fetch_partsurfer_description("875507-B21")
    assert desc == "HPE 240GB SATA 6G READ INTENSIVE SFF RW PM883 SSD"


@pytest.mark.asyncio
async def test_not_found_fixture_returns_none():
    # A real-shaped Search.aspx page with no lblDescription span → None (no guess).
    resp = _resp(_fixture("notfound.html"))
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        assert await partsurfer_resolver.fetch_partsurfer_description("ZZZNOTAPART999") is None


@pytest.mark.asyncio
async def test_non_200_returns_none():
    resp = _resp(_fixture("dimm_726719-B21.html"), status_code=503)
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        assert await partsurfer_resolver.fetch_partsurfer_description("726719-B21") is None


@pytest.mark.asyncio
async def test_http_error_is_swallowed_to_none():
    # ANY httpx error must be caught and returned as None — never raised into the worker.
    boom = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    with patch.object(partsurfer_resolver.http_redirect, "get", new=boom):
        assert await partsurfer_resolver.fetch_partsurfer_description("726719-B21") is None


@pytest.mark.asyncio
async def test_empty_lbldescription_returns_none():
    # The span is present but empty/whitespace → None (an empty string is not a description).
    resp = _resp('<span id="ctl00_BodyContentPlaceHolder_lblDescription">   </span>')
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        assert await partsurfer_resolver.fetch_partsurfer_description("726719-B21") is None


@pytest.mark.asyncio
async def test_blank_spare_pn_returns_none_without_fetching():
    # A blank spare can't be looked up — short-circuit, no HTTP call.
    get = AsyncMock()
    with patch.object(partsurfer_resolver.http_redirect, "get", new=get):
        assert await partsurfer_resolver.fetch_partsurfer_description("  ") is None
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_unescapes_html_entities_in_description():
    resp = _resp('<span id="ctl00_BodyContentPlaceHolder_lblDescription">HPE 2.5&quot; SFF &amp; LFF</span>')
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        desc = await partsurfer_resolver.fetch_partsurfer_description("123456-B21")
    assert desc == 'HPE 2.5" SFF & LFF'

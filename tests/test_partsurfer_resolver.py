"""tests/test_partsurfer_resolver.py -- fetch_partsurfer_description unit tests.

Covers: app/services/enrichment_worker/partsurfer_resolver.py. Patches the shared
``app.http_client.http_redirect.get`` with an AsyncMock so ZERO live network is touched —
the two real fixtures (a DIMM + an SSD captured from partsurfer.hpe.com) drive the happy
path; the fabricated not-found fixture and a genuine non-200 (e.g. 404) drive the
no-result contract (→ None). The throttle/outage contract is separate: a 429/5xx response
or a raised httpx error must raise ``PartSurferTransient`` so the caller backs off this
batch instead of mistaking a throttle for "no result".
Depends on: tests/fixtures/partsurfer/*.html.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.enrichment_worker import partsurfer_resolver
from app.services.enrichment_worker.partsurfer_resolver import PartSurferTransient

_FIXTURES = Path(__file__).parent / "fixtures" / "partsurfer"

# The honest contact UA the fetcher must send (matches the module constant).
_EXPECTED_UA = "AvailAI-PartLookup/1.0 (+sourcing enrichment; contact mkhoury@trioscs.com)"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _resp(text: str, status_code: int = 200):
    """A minimal stand-in for httpx.Response: only .status_code + .text are read."""
    resp = type("Resp", (), {})()
    resp.status_code = status_code
    resp.text = text
    return resp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture", "spare_pn", "expected"),
    [
        pytest.param(
            "dimm_726719-B21.html",
            "726719-B21",
            "HPE 16GB (1X16GB) DUAL RANK X4 DDR4-2133 CAS-15-15-15 REGISTERED MEMORY KIT",
            id="dimm",
        ),
        pytest.param(
            "ssd_875507-B21.html",
            "875507-B21",
            "HPE 240GB SATA 6G READ INTENSIVE SFF RW PM883 SSD",
            id="ssd",
        ),
    ],
)
async def test_fixture_returns_exact_description(fixture: str, spare_pn: str, expected: str):
    resp = _resp(_fixture(fixture))
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        desc = await partsurfer_resolver.fetch_partsurfer_description(spare_pn)
    assert desc == expected


@pytest.mark.asyncio
async def test_outbound_request_uses_exact_url_and_contact_ua():
    # Outbound contract: the GET targets the Search.aspx URL with the spare as SearchText
    # and carries the honest contact User-Agent (robots.txt allows Search.aspx).
    resp = _resp(_fixture("dimm_726719-B21.html"))
    get = AsyncMock(return_value=resp)
    with patch.object(partsurfer_resolver.http_redirect, "get", new=get):
        await partsurfer_resolver.fetch_partsurfer_description("726719-B21")
    url = get.await_args.args[0]
    assert url == "https://partsurfer.hpe.com/Search.aspx?SearchText=726719-B21"
    assert get.await_args.kwargs["headers"]["User-Agent"] == _EXPECTED_UA


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resp", "spare_pn"),
    [
        # A real-shaped Search.aspx page with no lblDescription span → None (no guess).
        pytest.param(_resp(_fixture("notfound.html")), "ZZZNOTAPART999", id="not_found_fixture"),
        # A 404/3xx is a GENUINE no-result (not a throttle) → None, the spare moves on.
        pytest.param(_resp(_fixture("dimm_726719-B21.html"), status_code=404), "726719-B21", id="genuine_non_200"),
        # The span is present but empty/whitespace → None (an empty string is not a description).
        pytest.param(
            _resp('<span id="ctl00_BodyContentPlaceHolder_lblDescription">   </span>'), "726719-B21", id="empty_lbl"
        ),
    ],
)
async def test_no_result_returns_none(resp, spare_pn: str):
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        assert await partsurfer_resolver.fetch_partsurfer_description(spare_pn) is None


@pytest.mark.asyncio
async def test_503_raises_transient():
    # A 5xx is a throttle/outage → PartSurferTransient so the caller backs off this batch.
    resp = _resp(_fixture("dimm_726719-B21.html"), status_code=503)
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        with pytest.raises(PartSurferTransient):
            await partsurfer_resolver.fetch_partsurfer_description("726719-B21")


@pytest.mark.asyncio
async def test_429_raises_transient_and_logs_warning():
    # A 429 (rate-limited) is the canonical throttle signal → PartSurferTransient + WARNING.
    resp = _resp(_fixture("dimm_726719-B21.html"), status_code=429)
    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=resp)):
        with patch.object(partsurfer_resolver.logger, "warning") as warn:
            with pytest.raises(PartSurferTransient):
                await partsurfer_resolver.fetch_partsurfer_description("726719-B21")
    assert warn.called


@pytest.mark.asyncio
async def test_http_error_raises_transient():
    # ANY httpx transport error (timeout/connect/transient) → PartSurferTransient (back off),
    # NOT None — a throttle masquerading as a no-result would hammer the host.
    boom = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    with patch.object(partsurfer_resolver.http_redirect, "get", new=boom):
        with pytest.raises(PartSurferTransient):
            await partsurfer_resolver.fetch_partsurfer_description("726719-B21")


@pytest.mark.asyncio
async def test_invalid_url_returns_none():
    # Permanently-bad input for this spare — a retry can't help → genuine no-result (None).
    boom = AsyncMock(side_effect=httpx.InvalidURL("bad url"))
    with patch.object(partsurfer_resolver.http_redirect, "get", new=boom):
        assert await partsurfer_resolver.fetch_partsurfer_description("726719-B21") is None


@pytest.mark.asyncio
async def test_text_attribute_raises_returns_none():
    # A pathological 200 whose .text raises → swallowed to None (a parse failure on a 200 is
    # a genuine no-description, not a throttle — never raises PartSurferTransient).
    class _BadText:
        status_code = 200

        @property
        def text(self):
            raise ValueError("decode boom")

    with patch.object(partsurfer_resolver.http_redirect, "get", new=AsyncMock(return_value=_BadText())):
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

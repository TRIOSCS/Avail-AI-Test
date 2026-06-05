"""Gate tests for OEM cross-ref + description extractors (mocked Claude)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.enrichment_worker import oem_extractor
from app.services.enrichment_worker.oem_extractor import (
    cross_reference_mpn,
    extract_oem_description,
)
from app.utils.claude_errors import ClaudeError

XR_OK = {
    "resolved_mpn": "M393A2K40EB3-CWE",
    "manufacturer": "Samsung",
    "linkage_quote": "Lenovo FRU 01HW917 = Samsung M393A2K40EB3-CWE 16GB DDR4 RDIMM",
    "confidence": 0.95,
    "source_urls": ["https://support.lenovo.com/parts/01HW917"],
}


async def _xr(data):
    with patch.object(oem_extractor, "claude_json", new=AsyncMock(return_value=data)):
        return await cross_reference_mpn("01HW917", "01hw917", "lenovo")


@pytest.mark.asyncio
async def test_crossref_accept():
    r = await _xr(dict(XR_OK))
    assert r.status == "resolved"
    assert r.resolved_mpn == "M393A2K40EB3-CWE"
    assert r.linkage_source_domain == "support.lenovo.com"


@pytest.mark.asyncio
async def test_crossref_reject_untrusted_domain():
    r = await _xr({**XR_OK, "source_urls": ["https://reddit.com/r/homelab"]})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_reject_linkage_missing_resolved():
    # quote lacks the resolved MPN → linkage not sourced
    r = await _xr({**XR_OK, "linkage_quote": "Lenovo FRU 01HW917 memory module"})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_reject_linkage_missing_oem_code():
    r = await _xr({**XR_OK, "linkage_quote": "Samsung M393A2K40EB3-CWE 16GB DDR4 RDIMM"})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_reject_echo_mpn():
    r = await _xr(
        {
            **XR_OK,
            "resolved_mpn": "01HW917",
            "linkage_quote": "01HW917 01HW917",
        }
    )
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_reject_low_confidence():
    r = await _xr({**XR_OK, "confidence": 0.5})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_crossref_claude_error_propagates():
    with patch.object(oem_extractor, "claude_json", new=AsyncMock(side_effect=ClaudeError("down"))):
        with pytest.raises(ClaudeError):
            await cross_reference_mpn("01HW917", "01hw917", "lenovo")


DESC_OK = {
    "description": "ThinkSystem 16GB TruDDR4 2666MHz RDIMM",
    "manufacturer": "Lenovo",
    "category": "Memory Module",
    "datasheet_url": None,
    "confidence": 0.95,
    "exact_mpn_found": "01HW917",
    "source_urls": ["https://support.lenovo.com/parts/01HW917"],
}


async def _desc(data):
    with patch.object(oem_extractor, "claude_json", new=AsyncMock(return_value=data)):
        return await extract_oem_description("01HW917", "01hw917", "lenovo")


@pytest.mark.asyncio
async def test_desc_accept():
    r = await _desc(dict(DESC_OK))
    assert r.status == "oem_sourced"
    assert r.description.startswith("ThinkSystem")
    assert r.source_domains == ["support.lenovo.com"]


@pytest.mark.asyncio
async def test_desc_reject_untrusted_domain():
    r = await _desc({**DESC_OK, "source_urls": ["https://www.ebay.com/itm/123"]})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_desc_reject_mpn_mismatch():
    r = await _desc({**DESC_OK, "exact_mpn_found": "01HW918"})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_desc_reject_short_description():
    r = await _desc({**DESC_OK, "description": "RAM"})
    assert r.status == "failed"


@pytest.mark.asyncio
async def test_desc_claude_error_propagates():
    with patch.object(oem_extractor, "claude_json", new=AsyncMock(side_effect=ClaudeError("down"))):
        with pytest.raises(ClaudeError):
            await extract_oem_description("01HW917", "01hw917", "lenovo")

"""Gate tests for the OEM crosswalk resolver (mocked Claude, recorded fixtures).

Each of the five Python trust gates is asserted independently against recorded-style
PartSurfer resolution fixtures (tests/fixtures/oem_crosswalk/*.json — captured once,
scrubbed; ZERO live calls in CI). claude_json is patched at its source module, the
tests/test_oem_extractor.py pattern.
"""

import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from app.services.enrichment_worker import oem_crosswalk_resolver
from app.services.enrichment_worker.oem_crosswalk_resolver import resolve_oem_spare
from app.utils.claude_errors import ClaudeError

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "oem_crosswalk"


def fixture(name: str) -> dict:
    return json.loads((_FIXTURES / f"{name}.json").read_text())


async def _resolve(data, mpn="875942-001", norm="875942001", vendor="hpe"):
    with patch.object(oem_crosswalk_resolver, "claude_json", new=AsyncMock(return_value=data)):
        return await resolve_oem_spare(mpn, norm, vendor)


@pytest.mark.asyncio
async def test_resolved_happy_path_cpu_kit():
    r = await _resolve(fixture("resolved_cpu_kit"))
    assert r.status == "resolved"
    assert r.canonical_mpn == "CD8067303409000"
    assert r.manufacturer == "Intel"
    assert r.title == "Intel Xeon-Gold 6130 (2.1GHz/16-core/125W) FIO processor kit"
    assert r.source_domain == "partsurfer.hp.com"
    assert r.source_url.startswith("https://partsurfer.hp.com/")
    assert r.confidence == 0.96
    assert r.payload == fixture("resolved_cpu_kit")  # full raw extraction kept for forensics


@pytest.mark.asyncio
async def test_resolved_happy_path_hdd_spare():
    r = await _resolve(fixture("resolved_hdd_spare"), mpn="695510-B21", norm="695510b21")
    assert r.status == "resolved"
    assert r.canonical_mpn == "ST4000NM0035"
    assert r.manufacturer == "Seagate"


@pytest.mark.asyncio
async def test_gate1_off_domain_is_no_match():
    r = await _resolve(fixture("off_domain"))
    assert r.status == "no_match"
    assert r.canonical_mpn is None
    assert r.payload == fixture("off_domain")  # forensics kept on negative outcomes too


@pytest.mark.asyncio
async def test_gate2_quote_missing_canonical_is_no_match():
    r = await _resolve(fixture("quote_missing_canonical"))
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate2_quote_missing_spare_is_no_match():
    r = await _resolve(fixture("quote_missing_spare"))
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate3_echo_is_no_match():
    r = await _resolve(fixture("echo"))
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate4_low_confidence_is_no_match():
    r = await _resolve(fixture("low_confidence"))
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate5_null_fields_is_no_match():
    r = await _resolve(fixture("null_fields"))
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate5_malformed_confidence_is_no_match():
    # A non-numeric confidence must degrade to no_match, never raise.
    r = await _resolve({**fixture("resolved_cpu_kit"), "confidence": "very sure"})
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate5_non_dict_response_is_no_match():
    assert (await _resolve(None)).status == "no_match"
    assert (await _resolve(["not", "a", "dict"])).status == "no_match"


@pytest.mark.asyncio
async def test_gate5_missing_source_urls_is_no_match():
    r = await _resolve({**fixture("resolved_cpu_kit"), "source_urls": None})
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_empty_normalized_mpn_short_circuits_no_match():
    mock = AsyncMock(return_value=fixture("resolved_cpu_kit"))
    with patch.object(oem_crosswalk_resolver, "claude_json", new=mock):
        r = await resolve_oem_spare("875942-001", "", "hpe")
    assert r.status == "no_match"
    mock.assert_not_awaited()  # never spends a web call on an un-normalizable spare


@pytest.mark.asyncio
async def test_claude_error_propagates():
    # Transient backend failure must reach the caller (which writes NO row) — it must
    # NOT be swallowed into a 90-day no_match.
    with patch.object(oem_crosswalk_resolver, "claude_json", new=AsyncMock(side_effect=ClaudeError("boom"))):
        with pytest.raises(ClaudeError):
            await resolve_oem_spare("875942-001", "875942001", "hpe")

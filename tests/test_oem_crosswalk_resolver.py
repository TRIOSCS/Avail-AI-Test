"""Gate tests for the OEM crosswalk resolver (mocked Claude, recorded fixtures).

Each of the five Python trust gates is asserted independently against recorded-style
PartSurfer resolution fixtures (tests/fixtures/oem_crosswalk/*.json — captured once,
scrubbed; ZERO live calls in CI), plus an adversarial-hallucination matrix: because a
resolution here is minted into a PERMANENT cache with no downstream distributor re-
verification, the verbatim gate must reject title fragments, truncated codes and cross-
token spans — not just wholesale fabrications. claude_json is patched at its source
module, the tests/test_oem_extractor.py pattern.
"""

import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest

from app.services.enrichment_worker import oem_crosswalk_resolver
from app.services.enrichment_worker.oem_crosswalk_resolver import OemResolveResult, resolve_oem_spare
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


@pytest.mark.parametrize(
    "override",
    [
        # Gate 1: the contract is a SINGLE source_url — the page the quote was taken from.
        # A missing source_url, or an untrusted quote source (even though the model could
        # have listed an unrelated trusted URL under the old list contract), must fail —
        # provenance must never be misattributed to a trusted domain.
        pytest.param({"source_url": None}, id="gate1_missing_source_url"),
        pytest.param({"source_url": "https://evil.example/spare_lookup"}, id="gate1_off_domain_quote_source"),
        # Gate 2 — verbatim-quote token gate (adversarial canonical_mpn shapes):
        #   'Gold 6130' → 'gold6130' is a substring of the collapsed quote but NOT a whole
        #     token ('Xeon-Gold' + '6130' are separate) — the most plausible CPU-cohort LLM
        #     failure (marketing model name instead of orderable tray MPN).
        #   '125W FIO' → '125wfio' exists in the collapsed quote ONLY because separator-
        #     stripping glues adjacent tokens together.
        #   '8067303409' is a truncation of the REAL canonical ('CD8067303409000') — a
        #     substring of the collapsed quote but not a token.
        pytest.param({"canonical_mpn": "Gold 6130"}, id="gate2_title_fragment"),
        pytest.param({"canonical_mpn": "125W FIO"}, id="gate2_cross_token_span"),
        pytest.param({"canonical_mpn": "8067303409"}, id="gate2_truncated_real"),
        # Gate 2 — canonical_mpn shape guard, applied before the quote is even consulted:
        #   'AB' (<6 chars) would substring-match virtually any normalized page text.
        #   >64 chars cannot fit canonical_mpn_raw (String(64)) and is garbage anyway —
        #     rejected in Python, never left for PostgreSQL to raise DataError on.
        pytest.param({"canonical_mpn": "AB"}, id="gate2_short_shape_guard"),
        pytest.param({"canonical_mpn": "X" * 65}, id="gate2_overlong_shape_guard"),
        # Gate 5: a non-numeric confidence must degrade to no_match, never raise.
        pytest.param({"confidence": "very sure"}, id="gate5_malformed_confidence"),
    ],
)
@pytest.mark.asyncio
async def test_resolved_cpu_kit_override_is_no_match(override):
    r = await _resolve({**fixture("resolved_cpu_kit"), **override})
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate2_quote_missing_canonical_is_no_match():
    r = await _resolve(fixture("quote_missing_canonical"))
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate2_quote_missing_spare_is_no_match():
    r = await _resolve(fixture("quote_missing_spare"))
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate2_quote_none_or_missing_is_no_match_without_raising():
    # The gate-5 'never raises on a parsed dict's shape' contract, pinned for the
    # quote field specifically (token extraction must tolerate None/missing).
    r = await _resolve({**fixture("resolved_cpu_kit"), "quote": None})
    assert r.status == "no_match"
    data = fixture("resolved_cpu_kit")
    del data["quote"]
    assert (await _resolve(data)).status == "no_match"


@pytest.mark.asyncio
async def test_gate3_echo_is_no_match():
    r = await _resolve(fixture("echo"))
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate3_truncated_spare_echo_is_no_match():
    # Adversarial: '75942-001' is a truncation of the spare itself — its norm is a
    # proper substring of the spare's norm. Both the token gate and the containment
    # no-echo gate must reject it; pin via a quote that prints it as a real token.
    data = {
        **fixture("resolved_cpu_kit"),
        "canonical_mpn": "75942-001",
        "quote": "875942-001 SPS-CPU Intel Xeon-Gold 6130 processor kit 75942-001",
    }
    r = await _resolve(data)
    assert r.status == "no_match"


@pytest.mark.asyncio
async def test_gate3_extension_of_spare_is_no_match():
    # Adversarial mirror: the spare norm CONTAINED IN the claimed canonical
    # ('875942-001X') is an echo-with-suffix, not a cross-reference.
    data = {
        **fixture("resolved_cpu_kit"),
        "canonical_mpn": "875942-001X",
        "quote": "875942-001 SPS-CPU Intel Xeon-Gold 6130 processor kit 875942-001X",
    }
    r = await _resolve(data)
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
async def test_unparseable_response_is_transient_not_cached():
    # claude_json returns None on empty/truncated/unparseable output (max_tokens
    # exhausted mid-JSON, tool-only turns) — that is TRANSIENT, not evidence the OEM
    # doesn't catalogue the spare. It must raise (caller writes NO row, free retry
    # next batch), never become a 90-day no_match with payload=None.
    with pytest.raises(ClaudeError):
        await _resolve(None)
    with pytest.raises(ClaudeError):
        await _resolve(["not", "a", "dict"])


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


def test_result_rejects_illegal_resolved_shapes():
    # The frozen dataclass refuses to represent states the resolver never produces —
    # resolved REQUIRES canonical_mpn + source_url + confidence >= 0.90, so no writer
    # can ever mint a row violating ck_oem_crosswalk_status_canonical.
    with pytest.raises(ValueError):
        OemResolveResult(status="resolved")
    with pytest.raises(ValueError):
        OemResolveResult(status="resolved", canonical_mpn="ST4000NM0035", source_url=None, confidence=0.95)
    with pytest.raises(ValueError):
        OemResolveResult(
            status="resolved", canonical_mpn="ST4000NM0035", source_url="https://x.example", confidence=0.5
        )
    # Legal shapes construct fine.
    OemResolveResult(status="resolved", canonical_mpn="ST4000NM0035", source_url="https://x.example", confidence=0.95)
    OemResolveResult(status="no_match")

"""tests/test_source_ingest_ai_correct.py — SP-Ingest ai_correct (no fabrication).

Covers: app/services/source_ingest/ai_correct.py — mocks claude_structured to verify the
no-fabrication contract (null value = not stated → never persisted), the trio_source_ai tag,
AI-category only when source category is missing, per-PART failure isolation with visible
corrected/failed counts, fail-fast on deterministic config/auth errors, and the
consecutive-failure abort.
"""

from __future__ import annotations

import pytest

import app.services.source_ingest.ai_correct as ai_mod
from app.services.source_ingest.ai_correct import AI_SOURCE, ai_correct
from app.services.source_ingest.models import ConsolidatedPart
from app.utils.claude_client import ClaudeAuthError, ClaudeUnavailableError


def _part(**kw) -> ConsolidatedPart:
    base = dict(normalized_mpn="st4000nm0035", raw_mpn="ST4000NM0035", description="4TB 7.2K SAS 3.5in HDD")
    base.update(kw)
    return ConsolidatedPart(**base)


def test_ai_source_tag():
    assert AI_SOURCE == "trio_source_ai"


@pytest.mark.asyncio
async def test_ai_correct_standardizes_and_extracts(monkeypatch):
    async def fake(prompt, schema, **kw):
        # Source says 4TB/7.2K/3.5in — model returns those plus a null for an absent field.
        return {
            "normalized_mpn": "st4000nm0035",
            "standardized_description": '4TB 7200RPM SAS 3.5" Enterprise HDD',
            "category": "hdd",
            "category_confidence": 0.95,
            "specs": [
                {"key": "capacity_gb", "value": 4000, "confidence": 0.98},
                {"key": "rpm", "value": "7200", "confidence": 0.9},
                {"key": "interface", "value": None, "confidence": 0.0},  # NOT stated → drop
            ],
        }

    monkeypatch.setattr(ai_mod, "claude_structured", fake)
    part = _part(category=None)  # no source category → AI may infer
    stats = await ai_correct([part])
    assert stats == {"corrected": 1, "failed": 0}
    assert part.ai_description == '4TB 7200RPM SAS 3.5" Enterprise HDD'
    assert part.ai_category == "hdd"
    assert part.ai_category_confidence == 0.95
    assert part.ai_specs["capacity_gb"] == {"value": 4000, "confidence": 0.98}
    assert part.ai_specs["rpm"] == {"value": "7200", "confidence": 0.9}
    assert "interface" not in part.ai_specs  # null value never persisted (no fabrication)


@pytest.mark.asyncio
async def test_ai_correct_does_not_override_existing_category(monkeypatch):
    async def fake(prompt, schema, **kw):
        return {
            "normalized_mpn": "x",
            "standardized_description": None,
            "category": "ssd",  # model proposes a category…
            "category_confidence": 0.9,
            "specs": [],
        }

    monkeypatch.setattr(ai_mod, "claude_structured", fake)
    part = _part(category="hdd")  # …but the source already has one → AI category ignored
    await ai_correct([part])
    assert part.ai_category is None


@pytest.mark.asyncio
async def test_ai_correct_isolates_failures_per_part_and_counts_them(monkeypatch):
    calls = {"n": 0}

    async def flaky(prompt, schema, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"normalized_mpn": "y", "standardized_description": "clean", "category": None, "specs": []}

    monkeypatch.setattr(ai_mod, "claude_structured", flaky)
    p1 = _part(normalized_mpn="a", raw_mpn="A")
    p2 = _part(normalized_mpn="b", raw_mpn="B")
    stats = await ai_correct([p1, p2])
    # First part's failure isolated (keeps non-AI values); second still corrected — and the
    # stats make the failure VISIBLE (the report prints ok/failed, never a silent zero).
    assert stats == {"corrected": 1, "failed": 1}
    assert p1.ai_description is None
    assert p2.ai_description == "clean"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [ClaudeUnavailableError("no key"), ClaudeAuthError("401")])
async def test_ai_correct_fails_fast_on_config_auth_errors(monkeypatch, exc):
    # Deterministic config/auth failures hit EVERY part identically — a missing API key
    # must abort the run on part 1, not grind through hundreds of thousands of parts.
    calls = {"n": 0}

    async def dead(prompt, schema, **kw):
        calls["n"] += 1
        raise exc

    monkeypatch.setattr(ai_mod, "claude_structured", dead)
    parts = [_part(normalized_mpn="a", raw_mpn="A"), _part(normalized_mpn="b", raw_mpn="B")]
    with pytest.raises(type(exc)):
        await ai_correct(parts)
    assert calls["n"] == 1  # aborted immediately — the second part was never attempted


@pytest.mark.asyncio
async def test_ai_correct_aborts_after_consecutive_failure_streak(monkeypatch):
    async def always_boom(prompt, schema, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_mod, "claude_structured", always_boom)
    monkeypatch.setattr(ai_mod, "_MAX_CONSECUTIVE_FAILURES", 3)
    parts = [_part(normalized_mpn=f"p{i}", raw_mpn=f"P{i}") for i in range(10)]
    with pytest.raises(RuntimeError, match="consecutive"):
        await ai_correct(parts)


@pytest.mark.asyncio
async def test_ai_correct_returns_null_when_source_silent(monkeypatch):
    # The headline guardrail: source lacks specs → model returns all-null → nothing fabricated.
    async def fake(prompt, schema, **kw):
        return {
            "normalized_mpn": "z",
            "standardized_description": None,
            "category": None,
            "specs": [{"key": "capacity_gb", "value": None, "confidence": 0.0}],
        }

    monkeypatch.setattr(ai_mod, "claude_structured", fake)
    part = _part(description="mystery part, no specs", category=None)
    await ai_correct([part])
    assert part.ai_description is None
    assert part.ai_category is None
    assert part.ai_specs == {}

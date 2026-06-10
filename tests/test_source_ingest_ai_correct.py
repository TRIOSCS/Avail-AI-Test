"""tests/test_source_ingest_ai_correct.py — SP-Ingest ai_correct (no fabrication).

Covers: app/services/source_ingest/ai_correct.py — mocks claude_structured to verify the
no-fabrication contract (null value = not stated → never persisted), the trio_source_ai tag,
AI-category only when source category is missing, and per-batch failure isolation.
"""

from __future__ import annotations

import pytest

import app.services.source_ingest.ai_correct as ai_mod
from app.services.source_ingest.ai_correct import AI_SOURCE, ai_correct
from app.services.source_ingest.models import ConsolidatedPart


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
    [out] = await ai_correct([part])
    assert out.ai_description == '4TB 7200RPM SAS 3.5" Enterprise HDD'
    assert out.ai_category == "hdd"
    assert out.ai_category_confidence == 0.95
    assert out.ai_specs["capacity_gb"] == {"value": 4000, "confidence": 0.98}
    assert out.ai_specs["rpm"] == {"value": "7200", "confidence": 0.9}
    assert "interface" not in out.ai_specs  # null value never persisted (no fabrication)


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
    [out] = await ai_correct([part])
    assert out.ai_category is None


@pytest.mark.asyncio
async def test_ai_correct_isolates_failures(monkeypatch):
    calls = {"n": 0}

    async def flaky(prompt, schema, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return {"normalized_mpn": "y", "standardized_description": "clean", "category": None, "specs": []}

    monkeypatch.setattr(ai_mod, "claude_structured", flaky)
    p1 = _part(normalized_mpn="a", raw_mpn="A")
    p2 = _part(normalized_mpn="b", raw_mpn="B")
    out = await ai_correct([p1, p2])
    # First part's failure isolated (keeps non-AI values); second still corrected.
    assert out[0].ai_description is None
    assert out[1].ai_description == "clean"


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
    [out] = await ai_correct([part])
    assert out.ai_description is None
    assert out.ai_category is None
    assert out.ai_specs == {}

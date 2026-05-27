"""Unit tests for the SpecCodeResolver service.

Covers every branch of resolve(): table hit, pending hit, blacklist propagation, LLM
empty AVL, confidence floor + web-search penalty, LLM failure modes, and concurrent-
insert recovery via UNIQUE-constraint collision.

Mocks the LLM call (claude_json) via the constructor's claude_call injection point. The
DB session comes from the autouse db_session fixture in tests/conftest.py — the resolver
and the test share the same session, which is what makes the concurrent-insert
simulation work (committing a competing row in the same session still triggers the
UNIQUE constraint at resolver-commit time).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
)
from app.services.spec_code_resolver import ResolverResult, SpecCodeResolver

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def resolver(db_session):
    """Resolver bound to the test session; LLM is unset until a test wires it."""
    return SpecCodeResolver(db_session, claude_call=AsyncMock())


@pytest.fixture
def approved_mapping(db_session):
    row = OemSpecCode(
        oem="IBM",
        spec_code="SPREJ",
        avl=[{"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}],
        source="manual",
        approved_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def pending_mapping(db_session):
    row = OemSpecCodePending(
        oem="IBM",
        spec_code="SPREJ",
        proposed_avl=[{"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": None}],
        llm_confidence=0.7,
        citations=[],
    )
    db_session.add(row)
    db_session.commit()
    return row


# ── Table-hit branch (Task 3.4) ──────────────────────────────────────


async def test_resolves_from_table_without_llm_call(resolver, approved_mapping):
    claude_mock = AsyncMock()
    resolver._claude_call = claude_mock

    result = await resolver.resolve("SPREJ")

    assert isinstance(result, ResolverResult)
    assert result.status == "approved"
    assert result.source == "table"
    assert result.confidence == 1.0
    assert result.avl[0]["mpn"] == "GRM188R71H103KA01D"
    claude_mock.assert_not_called()


async def test_normalizes_case_and_whitespace(resolver, approved_mapping):
    result = await resolver.resolve("  sprej  ", oem="ibm")
    assert result.status == "approved"


# ── Pending-hit branch (Task 3.5) ────────────────────────────────────


async def test_reuses_pending_row_without_llm_call(resolver, pending_mapping):
    claude_mock = AsyncMock()
    resolver._claude_call = claude_mock

    result = await resolver.resolve("SPREJ")

    assert result.status == "pending"
    assert result.source == "llm"
    assert result.confidence == 0.7
    assert result.avl[0]["mpn"] == "GRM188R71H103KA01D"
    claude_mock.assert_not_called()


# ── Blacklist propagation (Task 3.6) ─────────────────────────────────


async def test_blacklist_passes_into_llm_prompt(resolver, db_session):
    db_session.add(
        OemSpecCodeBlacklist(
            oem="IBM",
            spec_code="SPREJ",
            rejected_mpns=["BAD_MPN_1", "BAD_MPN_2"],
            reason="wrong package",
        )
    )
    db_session.commit()

    captured: dict = {}

    async def fake_claude(**kwargs):
        captured.update(kwargs)
        return {
            "avl": [{"mpn": "GOOD_MPN", "manufacturer": "Murata", "rank": 1, "notes": None}],
            "confidence": 0.9,
            "citations": [{"url": "https://example.com", "snippet": "..."}],
            "reasoning": "ok",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("SPREJ")

    assert "BAD_MPN_1" in captured["user"]
    assert "BAD_MPN_2" in captured["user"]
    assert result.status == "pending"
    assert result.source == "llm"
    assert result.avl[0]["mpn"] == "GOOD_MPN"


# ── LLM empty AVL → unresolved (Task 3.7) ────────────────────────────


async def test_empty_avl_treated_as_unresolved(resolver):
    async def fake_claude(**kwargs):
        return {"avl": [], "confidence": 0.0, "citations": [], "reasoning": ""}

    resolver._claude_call = fake_claude
    result = await resolver.resolve("UNKNOWN")
    assert result.status == "unresolved"

    # And no pending row should have been written
    assert resolver._db.query(OemSpecCodePending).count() == 0


# ── Confidence floor + WebSearch penalty (Task 3.8) ──────────────────


async def test_confidence_below_floor_unresolved(resolver):
    async def fake_claude(**kwargs):
        return {
            "avl": [{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            "confidence": 0.2,  # below default floor 0.3
            "citations": [{"url": "https://example.com", "snippet": "..."}],
            "reasoning": "low",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"


async def test_no_citations_applies_penalty(resolver):
    """Confidence 0.5 with no citations → 0.5 * 0.7 = 0.35 ≥ floor 0.3."""

    async def fake_claude(**kwargs):
        return {
            "avl": [{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            "confidence": 0.5,
            "citations": [],
            "reasoning": "no citations",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "pending"
    assert result.confidence == pytest.approx(0.35)


async def test_no_citations_below_penalized_floor_unresolved(resolver):
    """Confidence 0.4 with no citations → 0.4 * 0.7 = 0.28 < floor 0.3."""

    async def fake_claude(**kwargs):
        return {
            "avl": [{"mpn": "X", "manufacturer": "M", "rank": 1, "notes": None}],
            "confidence": 0.4,
            "citations": [],
            "reasoning": "no citations, just below penalized floor",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"


# ── LLM failure modes (Task 3.9) ─────────────────────────────────────


async def test_llm_exception_returns_unresolved(resolver):
    async def fake_claude(**kwargs):
        raise RuntimeError("api down")

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"


async def test_llm_none_returns_unresolved(resolver):
    async def fake_claude(**kwargs):
        return None

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"


async def test_llm_schema_invalid_returns_unresolved(resolver):
    async def fake_claude(**kwargs):
        return {"avl": "not a list", "confidence": 0.9}  # invalid

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")
    assert result.status == "unresolved"


# ── Concurrent insert collision (Task 3.10) ──────────────────────────


async def test_default_claude_call_uses_configured_model_tier(monkeypatch):
    """Verify the resolver's default claude call routes through the configured model
    tier, not a hardcoded one."""
    from app.config import settings
    from app.services import spec_code_resolver as resolver_module

    monkeypatch.setattr(settings, "spec_resolver_model_tier", "opus")

    captured: dict = {}

    async def fake_claude_json(prompt, **kwargs):
        captured.update(kwargs)
        return None  # don't care about the return shape

    # _default_claude_call lazy-imports claude_json from app.utils.claude_client,
    # so patch it at the source module.
    import app.utils.claude_client as claude_client_module

    monkeypatch.setattr(claude_client_module, "claude_json", fake_claude_json)

    await resolver_module._default_claude_call(system="sys", user="usr", tools=[], max_tokens=100)
    assert captured["model_tier"] == "opus"


# ── Blacklist leak filter (defense in depth) ─────────────────────────


async def test_llm_proposing_blacklisted_mpn_is_filtered(resolver, db_session):
    """Defense in depth: if LLM leaks a blacklisted MPN, the resolver
    filters it before persisting."""
    db_session.add(
        OemSpecCodeBlacklist(
            oem="IBM",
            spec_code="SPREJ",
            rejected_mpns=["BAD_MPN"],
            reason="known bad",
        )
    )
    db_session.commit()

    async def fake_claude(**kwargs):
        return {
            "avl": [
                {"mpn": "BAD_MPN", "manufacturer": "Bad", "rank": 1, "notes": None},
                {"mpn": "GOOD_MPN", "manufacturer": "Good", "rank": 2, "notes": None},
            ],
            "confidence": 0.9,
            "citations": [{"url": "https://example.com", "snippet": "..."}],
            "reasoning": "ok",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("SPREJ")

    assert result.status == "pending"
    mpns = [e["mpn"] for e in result.avl]
    assert "BAD_MPN" not in mpns
    assert "GOOD_MPN" in mpns


async def test_llm_proposing_only_blacklisted_mpns_returns_unresolved(resolver, db_session):
    db_session.add(
        OemSpecCodeBlacklist(
            oem="IBM",
            spec_code="SPREJ",
            rejected_mpns=["BAD_MPN_1", "BAD_MPN_2"],
            reason="all bad",
        )
    )
    db_session.commit()

    async def fake_claude(**kwargs):
        return {
            "avl": [
                {"mpn": "BAD_MPN_1", "manufacturer": "X", "rank": 1, "notes": None},
                {"mpn": "BAD_MPN_2", "manufacturer": "Y", "rank": 2, "notes": None},
            ],
            "confidence": 0.95,
            "citations": [{"url": "https://example.com", "snippet": "..."}],
            "reasoning": "all blacklisted",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("SPREJ")

    assert result.status == "unresolved"
    assert resolver._db.query(OemSpecCodePending).count() == 0


async def test_concurrent_pending_insert_recovers_via_reread(resolver, db_session):
    """Simulate a second resolver running for the same spec code by inserting a pending
    row out-of-band right before the LLM commit.

    The resolver and the test share `db_session`, but the UNIQUE constraint on
    (oem, spec_code) fires at commit time regardless — the resolver's commit
    raises IntegrityError, and it must recover by re-reading the winning row.
    """

    async def fake_claude(**kwargs):
        # Sneak a competing row in just before the resolver commits its own
        db_session.add(
            OemSpecCodePending(
                oem="IBM",
                spec_code="FOO",
                proposed_avl=[{"mpn": "WINNER", "manufacturer": "M", "rank": 1, "notes": None}],
                llm_confidence=0.8,
                citations=[],
            )
        )
        db_session.commit()
        return {
            "avl": [{"mpn": "LOSER", "manufacturer": "M", "rank": 1, "notes": None}],
            "confidence": 0.9,
            "citations": [{"url": "https://example.com", "snippet": "..."}],
            "reasoning": "would have lost",
        }

    resolver._claude_call = fake_claude
    result = await resolver.resolve("FOO")

    assert result.status == "pending"
    assert result.source == "llm"
    assert result.avl[0]["mpn"] == "WINNER"  # the resolver re-read the winning row

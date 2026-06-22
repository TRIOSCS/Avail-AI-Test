"""SP3 AI screening service tests.

LLM calls are always mocked — never hit a real API in tests.
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount

# ── Fixture helpers ──────────────────────────────────────────────────


def _prospect(db: Session, **kw) -> ProspectAccount:
    p = ProspectAccount(
        name=kw.pop("name", f"Co {uuid.uuid4().hex[:6]}"),
        domain=kw.pop("domain", f"co-{uuid.uuid4().hex[:6]}.com"),
        status="suggested",
        discovery_source="clay",
        created_at=datetime.now(timezone.utc),
        **kw,
    )
    db.add(p)
    db.commit()
    return p


# ── Config tests ─────────────────────────────────────────────────────


def test_sp3_config_defaults():
    assert settings.ai_screen_enabled is False
    assert settings.ai_screen_min_match == 40
    assert settings.ai_screen_daily_cap == 200
    assert settings.ai_screen_web_search_enabled is False


# ── Screen service tests ─────────────────────────────────────────────


async def test_screen_prospect_pass(db_session, monkeypatch):
    """A well-grounded pass verdict writes scores and persists ai_screen."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "ai_screen_min_match", 40)

    p = _prospect(
        db_session,
        industry="Aerospace & Defense",
        naics_code="336412",
        employee_count_range="501-1000",
        enrichment_data={},
        readiness_signals={},
        contacts_preview=[{"name": "Jane VP", "title": "VP Procurement", "email": "j@co.com", "verified": True}],
    )

    verdict = {
        "trio_match_score": 85,
        "opportunity_score": 70,
        "excess_likelihood": 30,
        "verdict": "pass",
        "rationale": "Aerospace OEM with verified procurement contact.",
        "evidence": ["industry=Aerospace & Defense", "naics=336412", "contacts=1 verified VP"],
        "confidence": 80,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-18T00:00:00+00:00",
    }

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=verdict):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            with patch("app.cache.intel_cache.incr_count", return_value=1):
                result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "pass"
    db_session.refresh(p)
    assert p.trio_match_score == 85
    assert p.opportunity_score == 70
    assert p.enrichment_data["ai_screen"]["verdict"] == "pass"
    assert p.enrichment_data["ai_screen"]["rationale"] == "Aerospace OEM with verified procurement contact."


async def test_screen_prospect_screened_out(db_session, monkeypatch):
    """Match below min_match threshold → verdict is screened_out."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "ai_screen_min_match", 40)

    p = _prospect(db_session, industry="Retail", enrichment_data={}, readiness_signals={})

    verdict = {
        "trio_match_score": 15,
        "opportunity_score": 10,
        "excess_likelihood": 5,
        "verdict": "pass",  # LLM returned pass, but score < min_match → we override to screened_out
        "rationale": "Retail company, no electronics manufacturing.",
        "evidence": ["industry=Retail"],
        "confidence": 90,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-18T00:00:00+00:00",
    }

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=verdict):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            with patch("app.cache.intel_cache.incr_count", return_value=1):
                result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "screened_out"
    db_session.refresh(p)
    assert p.trio_match_score == 15
    assert p.enrichment_data["ai_screen"]["verdict"] == "screened_out"


async def test_screen_prospect_insufficient_data_sets_flag(db_session, monkeypatch):
    """insufficient_data verdict sets needs_more_enrichment flag, does not write
    scores."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)

    p = _prospect(db_session, industry=None, enrichment_data={}, readiness_signals={})

    verdict = {
        "trio_match_score": 0,
        "opportunity_score": 0,
        "excess_likelihood": 0,
        "verdict": "insufficient_data",
        "rationale": "No industry or firmographic data available.",
        "evidence": [],
        "confidence": 10,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-18T00:00:00+00:00",
    }

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=verdict):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            with patch("app.cache.intel_cache.incr_count", return_value=1):
                result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "insufficient_data"
    db_session.refresh(p)
    # Scores must NOT be written for insufficient_data
    assert (p.trio_match_score or 0) == 0
    assert p.enrichment_data["ai_screen"]["needs_more_enrichment"] is True


async def test_screen_prospect_daily_cap_blocks(db_session, monkeypatch):
    """When daily cap is hit, screen_prospect returns early without an LLM call."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 5)

    p = _prospect(db_session, enrichment_data={})

    from app.services import prospect_screening as ps

    mock_llm = AsyncMock()
    with patch.object(ps, "_call_screen_llm", mock_llm):
        with patch("app.cache.intel_cache.get_count", return_value=5):  # at cap
            result = await ps.screen_prospect(p, db_session)

    mock_llm.assert_not_called()
    assert result["verdict"] == "cap_reached"


async def test_screen_prospect_cache_hit_skips_llm(db_session, monkeypatch):
    """If ai_screen has a verdict whose grounding fingerprint matches, skip the LLM
    call."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)

    from app.services import prospect_screening as ps

    p = _prospect(db_session, industry="Aerospace & Defense", enrichment_data={})
    # Stamp the cached verdict with the fingerprint of the prospect's CURRENT grounding,
    # so the cache hit is valid (grounding unchanged since the last screen).
    cached_verdict = {
        "verdict": "pass",
        "trio_match_score": 80,
        "opportunity_score": 65,
        "rationale": "Already screened.",
        "evidence": ["industry=Aerospace"],
        "confidence": 85,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-18T00:00:00+00:00",
        "grounding_fingerprint": ps._grounding_fingerprint(p),
    }
    p.enrichment_data = {"ai_screen": cached_verdict}
    db_session.commit()

    mock_llm = AsyncMock()
    with patch.object(ps, "_call_screen_llm", mock_llm):
        result = await ps.screen_prospect(p, db_session)

    mock_llm.assert_not_called()
    assert result["verdict"] == "pass"


async def test_screen_prospect_rescreens_when_grounding_changes(db_session, monkeypatch):
    """A cached verdict is bypassed when new enrichment changes the grounding
    fingerprint."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "ai_screen_min_match", 40)

    from app.services import prospect_screening as ps

    # Prior screen stamped against the OLD grounding (industry only, no contacts).
    p = _prospect(db_session, industry="Aerospace & Defense", enrichment_data={})
    stale_verdict = {
        "verdict": "screened_out",
        "trio_match_score": 30,
        "opportunity_score": 20,
        "rationale": "Thin signals at the time.",
        "evidence": ["industry=Aerospace & Defense"],
        "confidence": 50,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-18T00:00:00+00:00",
        "grounding_fingerprint": ps._grounding_fingerprint(p),
    }
    p.enrichment_data = {"ai_screen": stale_verdict}
    db_session.commit()

    # Material new enrichment: a verified procurement decision-maker appears.
    p.contacts_preview = [
        {"name": "Jane VP", "title": "VP Procurement", "verified": True, "seniority": "decision_maker"}
    ]
    db_session.commit()

    fresh_verdict = {
        "trio_match_score": 88,
        "opportunity_score": 75,
        "excess_likelihood": 30,
        "verdict": "pass",
        "rationale": "Now has a verified procurement decision-maker.",
        "evidence": ["industry=Aerospace & Defense", "contacts=1 verified decision-maker"],
        "confidence": 85,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-19T00:00:00+00:00",
    }

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=fresh_verdict) as mock_llm:
        with patch("app.cache.intel_cache.get_count", return_value=0):
            with patch("app.cache.intel_cache.incr_count", return_value=1):
                result = await ps.screen_prospect(p, db_session)

    mock_llm.assert_called_once()
    assert result["verdict"] == "pass"
    db_session.refresh(p)
    assert p.trio_match_score == 88
    # Fingerprint is refreshed to match the new grounding.
    assert p.enrichment_data["ai_screen"]["grounding_fingerprint"] == ps._grounding_fingerprint(p)


async def test_screen_prospect_disabled_returns_skipped(db_session, monkeypatch):
    """When ai_screen_enabled=False, return skip immediately without LLM."""
    monkeypatch.setattr(settings, "ai_screen_enabled", False)
    p = _prospect(db_session, enrichment_data={})

    from app.services import prospect_screening as ps

    mock_llm = AsyncMock()
    with patch.object(ps, "_call_screen_llm", mock_llm):
        result = await ps.screen_prospect(p, db_session)

    mock_llm.assert_not_called()
    assert result["verdict"] == "disabled"


async def test_screen_prospect_grounding_check_bypasses_llm(db_session, monkeypatch):
    """Insufficient grounding with web_search disabled: early return without LLM call."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "ai_screen_web_search_enabled", False)

    p = _prospect(db_session, industry=None, naics_code=None, description=None, enrichment_data={})

    from app.services import prospect_screening as ps

    mock_llm = AsyncMock()
    with patch.object(ps, "_call_screen_llm", mock_llm):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            result = await ps.screen_prospect(p, db_session)

    mock_llm.assert_not_called()
    assert result["verdict"] == "insufficient_data"
    db_session.refresh(p)
    assert p.enrichment_data["ai_screen"]["needs_more_enrichment"] is True


async def test_screen_prospect_llm_returns_insufficient_data_with_grounding(db_session, monkeypatch):
    """When grounding is present but LLM says insufficient_data, scores are not
    written."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "ai_screen_min_match", 40)

    p = _prospect(db_session, industry="Software", enrichment_data={}, readiness_signals={})

    verdict = {
        "trio_match_score": 0,
        "opportunity_score": 0,
        "excess_likelihood": 0,
        "verdict": "insufficient_data",
        "rationale": "Industry is software — no electronics component need evident.",
        "evidence": ["industry=Software"],
        "confidence": 20,
        "model": "claude-sonnet-4-6",
        "screened_at": "2026-06-22T00:00:00+00:00",
    }

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=verdict):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            with patch("app.cache.intel_cache.incr_count", return_value=1):
                result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "insufficient_data"
    db_session.refresh(p)
    assert (p.trio_match_score or 0) == 0
    assert p.enrichment_data["ai_screen"]["needs_more_enrichment"] is True


async def test_screen_prospect_empty_llm_response(db_session, monkeypatch):
    """Empty LLM response returns error verdict gracefully."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)

    p = _prospect(db_session, industry="Aerospace", enrichment_data={})

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value={}):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "error"


# ── _grounding_is_sufficient direct tests ────────────────────────────────────


def test_grounding_sufficient_with_industry(db_session):
    from app.services.prospect_screening import _grounding_is_sufficient

    p = _prospect(db_session, industry="Aerospace")
    assert _grounding_is_sufficient(p) is True


def test_grounding_sufficient_with_naics(db_session):
    from app.services.prospect_screening import _grounding_is_sufficient

    p = _prospect(db_session, naics_code="336412")
    assert _grounding_is_sufficient(p) is True


def test_grounding_sufficient_with_description(db_session):
    from app.services.prospect_screening import _grounding_is_sufficient

    p = _prospect(db_session, description="Aerospace OEM.")
    assert _grounding_is_sufficient(p) is True


def test_grounding_sufficient_with_sam_gov(db_session):
    from app.services.prospect_screening import _grounding_is_sufficient

    p = _prospect(db_session, enrichment_data={"sam_gov": {"purpose": "defense"}})
    assert _grounding_is_sufficient(p) is True


def test_grounding_insufficient_no_data(db_session):
    from app.services.prospect_screening import _grounding_is_sufficient

    p = _prospect(db_session, industry=None, naics_code=None, description=None, enrichment_data={})
    assert _grounding_is_sufficient(p) is False


# ── _assemble_context branches ────────────────────────────────────────────────


def test_assemble_context_includes_decision_maker(db_session):
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        industry="Aerospace",
        contacts_preview=[
            {
                "name": "Jane VP",
                "title": "VP Procurement",
                "email": "j@co.com",
                "verified": True,
                "seniority": "decision_maker",
            }
        ],
    )
    ctx = _assemble_context(p)
    assert "decision-maker" in ctx
    assert "Jane VP" in ctx


def test_assemble_context_verified_contacts_no_dm(db_session):
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        contacts_preview=[
            {"name": "Bob Buyer", "title": "Buyer", "email": "b@co.com", "verified": True, "seniority": "staff"}
        ],
    )
    ctx = _assemble_context(p)
    assert "verified contact" in ctx


def test_assemble_context_sam_gov_data(db_session):
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        enrichment_data={
            "sam_gov": {
                "purpose": "Defense contractor",
                "naics_codes": [{"code": "336412", "description": "Aircraft Engine Parts", "primary": True}],
            }
        },
    )
    ctx = _assemble_context(p)
    assert "SAM.gov" in ctx
    assert "336412" in ctx


def test_assemble_context_news_signals(db_session):
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        enrichment_data={"recent_news": [{"title": "Company wins DoD contract"}, {"title": "Q2 expansion"}]},
    )
    ctx = _assemble_context(p)
    assert "Recent news" in ctx
    assert "DoD" in ctx


def test_assemble_context_hiring_signal(db_session):
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        readiness_signals={"hiring": {"type": "expanding", "count": 5}},
    )
    ctx = _assemble_context(p)
    assert "Hiring signal" in ctx


def test_assemble_context_trio_history(db_session):
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        historical_context={"quote_count": 3, "bought_before": True, "last_activity": "2025-01-15"},
    )
    ctx = _assemble_context(p)
    assert "3 quotes" in ctx
    assert "prior customer" in ctx


def test_assemble_context_events(db_session):
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        readiness_signals={"events": [{"type": "acquisition"}, {"type": "funding"}]},
    )
    ctx = _assemble_context(p)
    assert "Recent events" in ctx


async def test_screen_prospect_llm_error_is_fire_and_forget(db_session, monkeypatch):
    """LLM failure must not propagate — returns error verdict, prospect unchanged."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)

    # Provide industry so grounding check passes and we reach the LLM call
    p = _prospect(db_session, enrichment_data={}, trio_match_score=0, industry="Aerospace")

    from app.services import prospect_screening as ps

    with patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, side_effect=Exception("timeout")):
        with patch("app.cache.intel_cache.get_count", return_value=0):
            result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "error"
    db_session.refresh(p)
    assert (p.trio_match_score or 0) == 0  # scores NOT written on error

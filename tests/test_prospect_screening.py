"""SP3 AI screening service tests.

LLM calls are always mocked — never hit a real API in tests.
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import UTC, datetime
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
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(p)
    db.commit()
    return p


# ── Config tests ─────────────────────────────────────────────────────


def test_sp3_config_defaults(monkeypatch):
    """AI-screen config CODE defaults, independent of any ambient prod ``.env``.

    A fresh Settings is built with no env file and the relevant vars cleared so the
    assertions verify the in-code defaults even when pytest runs from a checkout that
    carries a prod ``.env`` (e.g. ``AI_SCREEN_ENABLED=true``).
    """
    from app.config import Settings

    for key in (
        "AI_SCREEN_ENABLED",
        "AI_SCREEN_MIN_MATCH",
        "AI_SCREEN_DAILY_CAP",
        "AI_SCREEN_WEB_SEARCH_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)

    s = Settings(_env_file=None)
    assert s.ai_screen_enabled is False
    assert s.ai_screen_min_match == 40
    assert s.ai_screen_daily_cap == 200
    assert s.ai_screen_web_search_enabled is False


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


# ── _assemble_context: branch coverage ───────────────────────────────────────


def test_assemble_context_all_branches(db_session):
    """Cover every conditional branch in _assemble_context."""
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        industry="Aerospace & Defense",
        naics_code="336412",
        employee_count_range="501-1000",
        revenue_range="$100M-$500M",
        hq_location="Denver, CO",
        description="Manufacturer of avionics systems.",
        enrichment_data={
            "sam_gov": {
                "purpose": "Manufacture avionics",
                "naics_codes": [
                    {"code": "336412", "description": "Aircraft Engine", "primary": True},
                ],
            },
            "recent_news": [{"title": "Wins DoD contract"}, {"title": "Q3 revenue up"}],
        },
        readiness_signals={
            "hiring": {"type": "growth"},
            "events": [{"type": "acquisition"}, {"type": "funding"}],
        },
        contacts_preview=[{"name": "Jane VP", "title": "VP Procurement", "verified": False}],
        historical_context={
            "quote_count": 3,
            "bought_before": True,
            "last_activity": "2025-06-01",
        },
    )
    db_session.commit()

    ctx = _assemble_context(p)
    assert "Aerospace" in ctx
    assert "336412" in ctx
    assert "501-1000" in ctx
    assert "$100M" in ctx
    assert "Denver" in ctx
    assert "avionics" in ctx
    assert "Manufacture avionics" in ctx
    assert "Aircraft Engine" in ctx
    assert "Wins DoD" in ctx
    assert "growth" in ctx
    assert "acquisition" in ctx
    assert "3 quotes" in ctx
    assert "prior customer" in ctx
    assert "2025-06-01" in ctx


def test_assemble_context_unverified_contacts(db_session):
    """elif contacts branch: contacts exist but none are verified."""
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        industry="Electronics",
        contacts_preview=[
            {"name": "Bob", "title": "Buyer", "verified": False},
            {"name": "Alice", "title": "Manager", "verified": False},
        ],
    )
    db_session.commit()

    ctx = _assemble_context(p)
    assert "unverified" in ctx


def test_assemble_context_historical_context_freeform(db_session):
    """Freeform historical_context branch (no standard keys)."""
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        industry="Industrial",
        historical_context={"notes": "Legacy SF account, high potential."},
    )
    db_session.commit()

    ctx = _assemble_context(p)
    assert "Historical context" in ctx


def test_assemble_context_decision_makers(db_session):
    """Contacts with seniority=decision_maker show DM summary."""
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        industry="Defense",
        contacts_preview=[
            {
                "name": "Jane VP",
                "title": "VP Procurement",
                "verified": True,
                "seniority": "decision_maker",
            }
        ],
    )
    db_session.commit()

    ctx = _assemble_context(p)
    assert "decision-maker" in ctx
    assert "Jane VP" in ctx


def test_assemble_context_verified_non_dm_contacts(db_session):
    """Contacts verified but no decision_maker → verified count shown."""
    from app.services.prospect_screening import _assemble_context

    p = _prospect(
        db_session,
        industry="Tech",
        contacts_preview=[
            {"name": "Bob", "title": "Engineer", "verified": True, "seniority": "individual"},
        ],
    )
    db_session.commit()

    ctx = _assemble_context(p)
    assert "verified contact" in ctx


# ── _call_screen_llm: direct test ────────────────────────────────────────────


async def test_call_screen_llm_calls_claude_structured(monkeypatch):
    from app.services import prospect_screening as ps

    fake_result = {
        "trio_match_score": 75,
        "opportunity_score": 60,
        "excess_likelihood": 20,
        "verdict": "pass",
        "rationale": "Strong fit.",
        "evidence": [],
        "confidence": 80,
    }

    with patch("app.utils.claude_client.claude_structured", AsyncMock(return_value=fake_result)) as mock_cs:
        result = await ps._call_screen_llm("Company: Test (test.com)")

    mock_cs.assert_called_once()
    assert result["verdict"] == "pass"


async def test_call_screen_llm_returns_empty_dict_when_none(monkeypatch):
    from app.services import prospect_screening as ps

    with patch("app.utils.claude_client.claude_structured", AsyncMock(return_value=None)):
        result = await ps._call_screen_llm("Company: Test (test.com)")

    assert result == {}


# ── screen_prospect: empty LLM response ──────────────────────────────────────


async def test_screen_prospect_empty_llm_response_returns_error(db_session, monkeypatch):
    from app.services import prospect_screening as ps

    monkeypatch.setattr(ps.settings, "ai_screen_enabled", True)
    monkeypatch.setattr(ps.settings, "ai_screen_daily_cap", 999)

    p = _prospect(db_session, industry="Aerospace", enrichment_data={})
    db_session.commit()

    with (
        patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value={}),
        patch("app.cache.intel_cache.get_count", return_value=0),
    ):
        result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "error"


# ── screen_prospect: insufficient_data from LLM (not early return) ────────────


async def test_screen_prospect_llm_insufficient_data_sets_flag(db_session, monkeypatch):
    """When LLM returns insufficient_data for a grounded prospect → sets
    needs_more_enrichment."""
    from app.services import prospect_screening as ps

    monkeypatch.setattr(ps.settings, "ai_screen_enabled", True)
    monkeypatch.setattr(ps.settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(ps.settings, "ai_screen_min_match", 40)

    # Grounding IS sufficient (industry set) so we reach the LLM, not the early return
    p = _prospect(db_session, industry="Aerospace & Defense", enrichment_data={})
    db_session.commit()

    verdict = {
        "trio_match_score": 0,
        "opportunity_score": 0,
        "excess_likelihood": 0,
        "verdict": "insufficient_data",
        "rationale": "Thin data.",
        "evidence": [],
        "confidence": 10,
    }

    with (
        patch.object(ps, "_call_screen_llm", new_callable=AsyncMock, return_value=verdict),
        patch("app.cache.intel_cache.get_count", return_value=0),
        patch("app.cache.intel_cache.incr_count", return_value=1),
    ):
        result = await ps.screen_prospect(p, db_session)

    assert result["verdict"] == "insufficient_data"
    db_session.refresh(p)
    assert p.enrichment_data["ai_screen"]["needs_more_enrichment"] is True
    # Scores NOT written for insufficient_data from LLM
    assert (p.trio_match_score or 0) == 0


# ── Migration 218: persisted ai_screen_verdict cache + SQL grid paths ─────────


def _verdict_cache_column(db: Session, prospect_id: int):
    """Read ai_screen_verdict straight from the table (not the ORM identity map),
    proving the listener actually flushed the cache column to the DB."""
    from sqlalchemy import text

    return db.execute(
        text("SELECT ai_screen_verdict FROM prospect_accounts WHERE id = :pid"),
        {"pid": prospect_id},
    ).scalar()


def test_listener_mirrors_ai_screen_verdict_on_orm_flush(db_session):
    """Setting enrichment_data['ai_screen']['verdict'] on an ORM flush writes through
    the persisted ai_screen_verdict cache column (migration 218 listener) — and a row
    never screened keeps a NULL mirror."""
    from sqlalchemy.orm.attributes import flag_modified

    p = _prospect(db_session, enrichment_data={})
    assert _verdict_cache_column(db_session, p.id) is None  # never screened → NULL

    p.enrichment_data = {**(p.enrichment_data or {}), "ai_screen": {"verdict": "screened_out"}}
    flag_modified(p, "enrichment_data")
    db_session.commit()
    assert _verdict_cache_column(db_session, p.id) == "screened_out"

    p.enrichment_data = {**p.enrichment_data, "ai_screen": {"verdict": "pass"}}
    flag_modified(p, "enrichment_data")
    db_session.commit()
    assert _verdict_cache_column(db_session, p.id) == "pass"


async def test_screen_prospect_syncs_verdict_cache_column(db_session, monkeypatch):
    """The screening worker path (screen_prospect → ORM commit) keeps the persisted
    ai_screen_verdict column in lockstep — the before_update listener fires because the
    service writes through the ORM, never raw SQL."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "ai_screen_min_match", 40)

    p = _prospect(db_session, industry="Retail", enrichment_data={}, readiness_signals={})
    verdict = {
        "trio_match_score": 15,
        "opportunity_score": 10,
        "excess_likelihood": 5,
        "verdict": "pass",  # below min_match → service overrides to screened_out
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
    assert _verdict_cache_column(db_session, p.id) == "screened_out"


class TestScreenVerdictSqlGrid:
    """AI-screen grid paths over the persisted ai_screen_verdict column (migration 218,
    audit M5+M6): flag-ON filters/sorts/paginates + buckets in SQL; flag-OFF
    unchanged."""

    @staticmethod
    def _seed(db, name, domain, trio=None, verdict=None):
        p = ProspectAccount(
            name=name,
            domain=domain,
            status="suggested",
            discovery_source="manual",
            created_at=datetime.now(UTC),
            trio_match_score=trio,
            enrichment_data={"ai_screen": {"verdict": verdict}} if verdict else {},
        )
        db.add(p)
        db.commit()
        return p

    @staticmethod
    def _grid_ctx(client, monkeypatch, url) -> dict:
        """GET the grid with template_response captured, returning the render ctx."""
        from fastapi.responses import HTMLResponse

        from app.routers.htmx import prospecting as router

        captured: dict = {}

        def _capture(template, ctx, *a, **k):
            captured["ctx"] = ctx
            return HTMLResponse("<html/>")

        monkeypatch.setattr(router, "template_response", _capture)
        resp = client.get(url)
        assert resp.status_code == 200
        return captured["ctx"]

    def test_flag_on_excludes_screened_out_and_buckets_with_true_count(self, client, db_session, monkeypatch):
        """Main grid = non-screened rows ranked by trio_match (name would sort the other
        way, proving score drives); screened-out rows land ONLY in the bucket, best-
        first, with an honest total.

        A never-screened row (NULL verdict) stays in the main grid.
        """
        monkeypatch.setattr("app.config.settings.ai_screen_enabled", True)
        self._seed(db_session, "ZZZ_TopPass", "top-218.com", trio=90, verdict="pass")
        self._seed(db_session, "MMM_Unscreened", "mid-218.com", trio=70)  # NULL verdict
        self._seed(db_session, "AAA_LowPass", "low-218.com", trio=50, verdict="pass")
        self._seed(db_session, "ZZZ_BucketTop", "so-top-218.com", trio=95, verdict="screened_out")
        self._seed(db_session, "AAA_BucketLow", "so-low-218.com", trio=10, verdict="screened_out")

        ctx = self._grid_ctx(client, monkeypatch, "/v2/partials/prospecting?sort=ai_match_desc")
        assert [x.name for x in ctx["prospects"]] == ["ZZZ_TopPass", "MMM_Unscreened", "AAA_LowPass"]
        assert ctx["total"] == 3
        assert [x.name for x in ctx["screened_out_prospects"]] == ["ZZZ_BucketTop", "AAA_BucketLow"]
        assert ctx["screened_out_total"] == 2

    def test_flag_on_paginates_in_sql_and_caps_bucket(self, client, db_session, monkeypatch):
        """Pagination happens in SQL (page 2 holds only the third-ranked row) and the
        bucket is LIMITed to _SCREENED_OUT_CAP best-first while screened_out_total stays
        the honest full count."""
        from app.routers.htmx import prospecting as router

        monkeypatch.setattr("app.config.settings.ai_screen_enabled", True)
        monkeypatch.setattr(router, "_SCREENED_OUT_CAP", 2)
        self._seed(db_session, "ZZZ_TopPass", "top-218.com", trio=90, verdict="pass")
        self._seed(db_session, "MMM_MidPass", "mid-218.com", trio=70, verdict="pass")
        self._seed(db_session, "AAA_LowPass", "low-218.com", trio=50, verdict="pass")
        self._seed(db_session, "ZZZ_SoTop", "so-a-218.com", trio=60, verdict="screened_out")
        self._seed(db_session, "MMM_SoMid", "so-b-218.com", trio=40, verdict="screened_out")
        self._seed(db_session, "AAA_SoLow", "so-c-218.com", trio=20, verdict="screened_out")

        ctx = self._grid_ctx(client, monkeypatch, "/v2/partials/prospecting?sort=ai_match_desc&per_page=2&page=2")
        assert [x.name for x in ctx["prospects"]] == ["AAA_LowPass"]
        assert ctx["total"] == 3
        assert ctx["total_pages"] == 2
        # Capped best-first, honest total beyond the cap.
        assert [x.name for x in ctx["screened_out_prospects"]] == ["ZZZ_SoTop", "MMM_SoMid"]
        assert ctx["screened_out_total"] == 3

    def test_flag_on_filters_on_cache_column_not_jsonb(self, client, db_session, monkeypatch):
        """A row whose JSONB says screened_out but whose cache column is NULL (a pre-
        backfill row) lands in the MAIN grid — pinning the persisted column, not a
        Python JSONB re-derive, as the filter source.

        Migration 218's PG backfill closes this gap for real rows; a regression back to
        the in-memory JSONB split fails here.
        """
        from sqlalchemy import text

        monkeypatch.setattr("app.config.settings.ai_screen_enabled", True)
        p = self._seed(db_session, "DriftRow", "drift-218.com", trio=80, verdict="screened_out")
        db_session.execute(text("UPDATE prospect_accounts SET ai_screen_verdict = NULL WHERE id = :pid"), {"pid": p.id})
        db_session.commit()
        db_session.expire_all()

        ctx = self._grid_ctx(client, monkeypatch, "/v2/partials/prospecting?sort=ai_match_desc")
        assert [x.name for x in ctx["prospects"]] == ["DriftRow"]
        assert ctx["screened_out_prospects"] == []
        assert ctx["screened_out_total"] == 0

    def test_flag_off_grid_unchanged_by_verdicts(self, client, db_session, monkeypatch):
        """AI-screen OFF: verdicts do not filter — screened-out rows stay in the main
        grid (still trio-ranked) and the bucket stays empty."""
        monkeypatch.setattr("app.config.settings.ai_screen_enabled", False)
        self._seed(db_session, "AAA_Screened", "so-218.com", trio=95, verdict="screened_out")
        self._seed(db_session, "ZZZ_TopPass", "top-218.com", trio=90, verdict="pass")
        self._seed(db_session, "MMM_Unscreened", "mid-218.com", trio=20)

        ctx = self._grid_ctx(client, monkeypatch, "/v2/partials/prospecting?sort=ai_match_desc")
        assert [x.name for x in ctx["prospects"]] == ["AAA_Screened", "ZZZ_TopPass", "MMM_Unscreened"]
        assert ctx["total"] == 3
        assert ctx["screened_out_prospects"] == []
        assert ctx["screened_out_total"] == 0

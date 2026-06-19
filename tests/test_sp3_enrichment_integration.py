"""SP3 integration: run_enrichment_job fires screen_prospect as final step.

All external calls (LLM, free enrichment, warm intros) are mocked.
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount


def _prospect(db: Session, **kw) -> ProspectAccount:
    p = ProspectAccount(
        name=f"Co {uuid.uuid4().hex[:6]}",
        domain=f"co-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        discovery_source="clay",
        created_at=datetime.now(timezone.utc),
        enrichment_data={},
        readiness_signals={},
        **kw,
    )
    db.add(p)
    db.commit()
    return p


async def test_run_enrichment_job_calls_screen_as_final_step(db_session, monkeypatch):
    """screen_prospect is called once, after fit/readiness recompute."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "prospect_enrich_contacts_per_account", 5)

    p = _prospect(db_session, industry="Aerospace & Defense", naics_code="336412")

    screen_calls: list[int] = []

    async def _fake_screen(prospect, db):
        screen_calls.append(prospect.id)
        return {"verdict": "pass", "trio_match_score": 80, "opportunity_score": 70}

    from app.services import prospect_free_enrichment as pfe
    from app.services import prospect_screening as ps

    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value=None),
        patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=[]),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
        patch.object(ps, "screen_prospect", new=_fake_screen),
    ):
        await pfe.run_enrichment_job(p.id, db=db_session)

    assert screen_calls == [p.id], "screen_prospect must be called exactly once"


async def test_run_enrichment_job_screen_error_does_not_corrupt_enrichment(db_session, monkeypatch):
    """A screen_prospect exception must not roll back the preceding enrichment data."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_daily_cap", 999)
    monkeypatch.setattr(settings, "prospect_enrich_contacts_per_account", 5)

    p = _prospect(db_session, industry=None)

    from app.services import prospect_free_enrichment as pfe
    from app.services import prospect_screening as ps

    async def _boom(prospect, db):
        raise RuntimeError("LLM timeout")

    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value=None),
        patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=[]),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
        patch.object(ps, "screen_prospect", new=_boom),
    ):
        # Must not raise
        await pfe.run_enrichment_job(p.id, db=db_session)

    db_session.refresh(p)
    # enrich_status should still be 'done' (screen failure is non-fatal)
    assert (p.enrichment_data or {}).get("enrich_status") == "done"


async def test_run_enrichment_job_screen_disabled_still_commits_enrichment(db_session, monkeypatch):
    """When ai_screen_enabled=False, run_enrichment_job completes normally."""
    monkeypatch.setattr(settings, "ai_screen_enabled", False)
    monkeypatch.setattr(settings, "prospect_enrich_contacts_per_account", 5)

    p = _prospect(db_session)

    from app.services import prospect_free_enrichment as pfe

    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value=None),
        patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=[]),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
    ):
        await pfe.run_enrichment_job(p.id, db=db_session)

    db_session.refresh(p)
    assert (p.enrichment_data or {}).get("enrich_status") == "done"

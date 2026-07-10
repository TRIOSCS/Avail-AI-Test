"""SP1 prospect adapter: real enrichment maps fill-only, infers seniority, recomputes scores.

enrich_entity / find_suggested_contacts are patched at the prospect_free_enrichment import
site (they're imported lazily inside run_enrichment_job, so patch the source module).
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount
from app.services.prospect_free_enrichment import (
    _apply_company_to_prospect,
    _apply_contacts_to_prospect,
    infer_seniority,
)


def _prospect(db: Session, **kw) -> ProspectAccount:
    p = ProspectAccount(
        name=f"P {uuid.uuid4().hex[:6]}",
        domain=f"p-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        discovery_source="clay",
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(p)
    db.commit()
    return p


def test_infer_seniority():
    assert infer_seniority("VP of Supply Chain") == "decision_maker"
    assert infer_seniority("Director, Procurement") == "decision_maker"
    assert infer_seniority("Senior Buyer") == "influencer"
    assert infer_seniority("Procurement Specialist") == "influencer"
    assert infer_seniority("Warehouse Associate") == "contributor"
    assert infer_seniority(None) == "contributor"


def test_apply_company_fill_only_preserves_sam_naics(db_session):
    p = _prospect(db_session, naics_code="336412", industry=None)
    _apply_company_to_prospect(
        p,
        {
            "industry": "Aerospace",
            "employee_size": "501-1000",
            "naics": "111111",
            "hq_city": "Dallas",
            "hq_state": "TX",
            "revenue_range": "$50M-$100M",
        },
    )
    assert p.naics_code == "336412"  # SAM.gov preserved (fill-only)
    assert p.industry == "Aerospace"  # was empty → filled
    assert p.employee_count_range == "501-1000"
    assert p.hq_location == "Dallas, TX"
    assert p.revenue_range == "$50M-$100M"


def test_apply_contacts_maps_and_counts(db_session):
    p = _prospect(db_session)
    mapped = _apply_contacts_to_prospect(
        p,
        [
            {"full_name": "Jane VP", "title": "VP Procurement", "email": "jane@a.com", "verified": True},
            {"full_name": "Joe Buyer", "title": "Buyer", "email": "joe@a.com"},  # verified default False
            {"full_name": "Jane VP", "title": "VP Procurement", "email": "jane@a.com", "verified": True},  # dup
        ],
        limit=5,
    )
    assert len(mapped) == 2  # deduped
    assert mapped[0]["seniority"] == "decision_maker"
    assert mapped[1]["verified"] is False
    assert p.contacts_preview == mapped


async def test_run_enrichment_job_paid_step_recomputes(db_session, monkeypatch):
    from app.services import prospect_free_enrichment as pfe

    monkeypatch.setattr(settings, "prospect_enrich_contacts_per_account", 5)
    p = _prospect(
        db_session,
        fit_score=10,
        readiness_score=10,
        industry=None,
        readiness_signals={
            "intent": {"strength": "strong"},
            "events": [{"type": "funding"}],
            "hiring": {"type": "procurement"},
        },
    )

    company = {
        "industry": "Aerospace & Defense",
        "naics": "336412",
        "employee_size": "501-1000",
        "hq_city": "Dallas",
        "hq_state": "TX",
        "revenue_range": "$100M+",
    }
    contacts = [{"full_name": "Jane VP", "title": "VP Procurement", "email": "jane@a.com", "verified": True}]

    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", new_callable=AsyncMock, return_value=company),
        patch("app.enrichment_service.find_suggested_contacts", new_callable=AsyncMock, return_value=contacts),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
    ):
        await pfe.run_enrichment_job(p.id, db=db_session)

    db_session.refresh(p)
    assert p.industry == "Aerospace & Defense"  # firmographic filled
    assert p.fit_score > 10  # recomputed (was 10)
    assert p.readiness_score >= 70  # strong signals + verified contact
    assert (p.readiness_signals or {}).get("contacts_verified_count") == 1
    assert (p.enrichment_data or {}).get("contact_provider")
    assert (p.enrichment_data or {}).get("contacts_enriched_at")
    assert (p.enrichment_data or {}).get("enrich_status") == "done"


async def test_run_enrichment_job_24h_skip(db_session, monkeypatch):
    from app.services import prospect_free_enrichment as pfe

    recent = datetime.now(UTC).isoformat()
    p = _prospect(
        db_session,
        fit_score=10,
        readiness_score=10,
        enrichment_data={"contacts_enriched_at": recent},
        readiness_signals={"intent": {"strength": "strong"}},
    )
    enrich_mock = AsyncMock()
    with (
        patch.object(pfe, "run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.enrichment_service.enrich_entity", enrich_mock),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
    ):
        await pfe.run_enrichment_job(p.id, db=db_session)

    enrich_mock.assert_not_called()  # within 24h → paid step skipped
    db_session.refresh(p)
    assert (p.enrichment_data or {}).get("enrich_status") == "done"  # still completes + recomputes

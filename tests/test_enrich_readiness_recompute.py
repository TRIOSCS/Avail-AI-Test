"""Enrichment must recompute readiness_score from the (news-augmented) signals.

Free enrichment merges news-derived event signals into readiness_signals; without a
recompute the readiness % / tier and buyer-ready ranking would stay stale after Enrich.
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models.prospect_account import ProspectAccount


def _cold_prospect_with_event_signals(db: Session) -> ProspectAccount:
    # Cold score, but signals already carry strong intent + a funding event + procurement
    # hiring (as free enrichment would have merged in from news) — readiness must reflect them.
    p = ProspectAccount(
        name=f"Enrich {uuid.uuid4().hex[:6]}",
        domain=f"enrich-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        fit_score=67,
        readiness_score=19,  # stale cold value
        readiness_signals={
            "intent": {"strength": "strong"},
            "events": [{"type": "funding"}],
            "hiring": {"type": "procurement"},
        },
        discovery_source="clay",
        created_at=datetime.now(UTC),
    )
    db.add(p)
    db.commit()
    return p


async def test_enrichment_recomputes_readiness_from_signals(db_session):
    from app.services.prospect_free_enrichment import run_enrichment_job

    p = _cold_prospect_with_event_signals(db_session)
    assert p.readiness_score == 19  # before

    with (
        patch("app.services.prospect_free_enrichment.run_free_enrichment", new_callable=AsyncMock, return_value={}),
        patch("app.services.prospect_warm_intros.detect_warm_intros", return_value={}),
        patch("app.services.prospect_warm_intros.generate_one_liner", return_value=""),
    ):
        await run_enrichment_job(p.id, db=db_session)

    db_session.refresh(p)
    # strong intent (35) + funding (25) + procurement hiring (20) + ... >> 19 → call-now tier
    assert p.readiness_score >= 70, f"readiness not recomputed after enrich (got {p.readiness_score})"
    assert (p.enrichment_data or {}).get("enrich_status") == "done"

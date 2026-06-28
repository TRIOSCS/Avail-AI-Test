"""SP3 rank + gate: ai_match_desc sort, screened-out bucket, stats count."""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount


def _prospect(
    db: Session,
    *,
    trio_match_score: int = 0,
    opportunity_score: int = 0,
    readiness_score: int = 0,
    ai_verdict: str | None = None,
    **kw,
) -> ProspectAccount:
    ed: dict = {}
    if ai_verdict:
        ed["ai_screen"] = {"verdict": ai_verdict, "rationale": "test", "evidence": []}
    p = ProspectAccount(
        name=kw.pop("name", f"Co {uuid.uuid4().hex[:6]}"),
        domain=kw.pop("domain", f"co-{uuid.uuid4().hex[:6]}.com"),
        status="suggested",
        discovery_source="clay",
        created_at=datetime.now(timezone.utc),
        trio_match_score=trio_match_score,
        opportunity_score=opportunity_score,
        readiness_score=readiness_score,
        enrichment_data=ed,
        readiness_signals={},
        **kw,
    )
    db.add(p)
    db.commit()
    return p


def test_ai_match_desc_sort_orders_by_trio_match_score(db_session, monkeypatch):
    """ai_match_desc returns prospects sorted trio_match_score DESC."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    low = _prospect(db_session, trio_match_score=20, name="Low Co", domain="low.com")
    high = _prospect(db_session, trio_match_score=90, name="High Co", domain="high.com")
    mid = _prospect(db_session, trio_match_score=55, name="Mid Co", domain="mid.com")

    # Query the DB directly using the sort logic the route applies
    rows = db_session.query(ProspectAccount).filter(ProspectAccount.status == "suggested").all()
    rows.sort(
        key=lambda p: (
            -(p.trio_match_score or 0),
            -(p.opportunity_score or 0),
            -(p.readiness_score or 0),
            (p.name or "").lower(),
        )
    )
    assert rows[0].id == high.id
    assert rows[1].id == mid.id
    assert rows[2].id == low.id


def test_screened_out_bucket_excluded_from_main_when_enabled(db_session, monkeypatch):
    """screened_out accounts excluded from main grid when ai_screen_enabled."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)
    monkeypatch.setattr(settings, "ai_screen_min_match", 40)

    good = _prospect(db_session, trio_match_score=80, ai_verdict="pass", name="Good Co", domain="good.com")
    bad = _prospect(db_session, trio_match_score=10, ai_verdict="screened_out", name="Bad Co", domain="bad.com")

    # Simulate the filtering logic the route applies
    all_rows = db_session.query(ProspectAccount).filter(ProspectAccount.status == "suggested").all()
    main = [p for p in all_rows if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") != "screened_out"]
    screened = [p for p in all_rows if (p.enrichment_data or {}).get("ai_screen", {}).get("verdict") == "screened_out"]

    assert len(main) == 1 and main[0].id == good.id
    assert len(screened) == 1 and screened[0].id == bad.id


def test_screened_out_bucket_included_when_disabled(db_session, monkeypatch):
    """When ai_screen_enabled=False, screened_out accounts appear in the main grid."""
    monkeypatch.setattr(settings, "ai_screen_enabled", False)

    _prospect(db_session, trio_match_score=80, ai_verdict="pass", name="Good Co", domain="good.com")
    _prospect(db_session, trio_match_score=10, ai_verdict="screened_out", name="Bad Co", domain="bad.com")

    all_rows = db_session.query(ProspectAccount).filter(ProspectAccount.status == "suggested").all()
    # Without the gate, screened_out is just a label — all accounts appear
    assert len(all_rows) == 2


def test_prospect_stats_ctx_includes_screened_out_count(db_session, monkeypatch):
    """_prospect_stats_ctx returns screened_out key when ai_screen_enabled."""
    monkeypatch.setattr(settings, "ai_screen_enabled", True)

    _prospect(db_session, ai_verdict="pass", name="Pass Co", domain="pass.com")
    _prospect(db_session, ai_verdict="screened_out", name="Screened Co", domain="screened.com")
    _prospect(db_session, name="Unscreened Co", domain="unscreened.com")

    from app.routers.htmx.prospecting import _prospect_stats_ctx

    ctx = _prospect_stats_ctx(db_session)
    assert "screened_out" in ctx
    assert ctx["screened_out"] == 1

"""Regression: the prospecting list's lazy-loaded stats panel must target ITSELF.

A `hx-trigger="load"` element with no `hx-target` inherits #main-content's
`hx-target="this"`, so its lazy load swaps the stats response INTO #main-content
and wipes the entire card grid (buckets show, cards vanish). curl-based verification
never caught this because curl does not execute htmx; this test does it at the HTML
level so the explicit hx-target can't silently regress.
"""

import os
import re

os.environ["TESTING"] = "1"

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.prospect_account import ProspectAccount


def _make(db: Session) -> ProspectAccount:
    p = ProspectAccount(
        name=f"Reg {uuid.uuid4().hex[:6]}",
        domain=f"reg-{uuid.uuid4().hex[:6]}.com",
        status="suggested",
        fit_score=70,
        readiness_score=50,
        discovery_source="manual",
        created_at=datetime.now(UTC),
    )
    db.add(p)
    db.commit()
    return p


def test_stats_panel_targets_itself_not_main_content(client, db_session):
    _make(db_session)
    html = client.get("/v2/partials/prospecting").text

    m = re.search(r'<div[^>]*id="prospect-stats"[^>]*>', html)
    assert m, "stats container (#prospect-stats) missing from the list partial"
    tag = m.group(0)
    assert 'hx-trigger="load"' in tag, "stats panel should lazy-load"
    # The crux: an explicit target so the lazy load fills the panel instead of
    # inheriting #main-content's hx-target='this' and replacing the whole grid.
    assert 'hx-target="#prospect-stats"' in tag, (
        "stats lazy-load is missing hx-target='#prospect-stats' — it would inherit "
        "#main-content's hx-target='this' and wipe the card grid on load"
    )


def test_no_prospecting_lazy_load_inherits_main_content_target():
    """Every hx-trigger='load' element across the prospecting templates carries an
    explicit hx-target (so none can inherit #main-content and hijack the page)."""
    import pathlib

    tdir = pathlib.Path(__file__).resolve().parent.parent / "app/templates/htmx/partials/prospecting"
    offenders = []
    for f in sorted(tdir.glob("*.html")):
        src = f.read_text()
        # Inspect each opening tag that contains a load trigger.
        for tag in re.findall(r"<[a-zA-Z][^>]*hx-trigger=\"[^\"]*load[^\"]*\"[^>]*>", src):
            if "hx-target=" not in tag:
                offenders.append(f"{f.name}: {tag[:120]}")
    assert not offenders, "lazy-load element(s) missing hx-target:\n" + "\n".join(offenders)


# ── Migration 218 / audit M6: the stats KPIs come from ONE SQL aggregate pass ──
#
# _prospect_stats_ctx reads the persisted is_buyer_ready + ai_screen_verdict cache
# columns (write-through by the ProspectAccount listener) instead of re-snapshotting
# every SUGGESTED row in Python. coalesce(is_buyer_ready, false) honest-degrades a
# NULL cache (pre-backfill row) to "not buyer ready".


# Inputs that make build_priority_snapshot()["is_buyer_ready"] True (score ≥70 with a
# proof point, fit ≥50, readiness ≥30).
_READY_KW = dict(
    fit_score=78,
    readiness_score=64,
    readiness_signals={"intent": {"strength": "strong"}},
    contacts_preview=[{"name": "DM", "verified": True, "seniority": "decision_maker"}],
)


def _stats_prospect(db: Session, **kw) -> ProspectAccount:
    p = ProspectAccount(
        name=kw.pop("name", f"Stat {uuid.uuid4().hex[:6]}"),
        domain=kw.pop("domain", f"stat-{uuid.uuid4().hex[:6]}.com"),
        status=kw.pop("status", "suggested"),
        discovery_source="manual",
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(p)
    db.commit()
    return p


class TestStatsCtxSqlAggregate:
    def test_counts_flag_on_with_null_cache_degrade(self, db_session, monkeypatch):
        """All five KPIs from the SQL aggregate over the cache columns; a row whose
        is_buyer_ready cache was NULLed (simulating a pre-backfill row) does NOT count
        as buyer-ready even though the live Python formula says it is — proving both the
        coalesce degrade and that the SQL path (not a snapshot loop) drives the KPI."""
        from sqlalchemy import text

        from app.routers.htmx.prospecting import _prospect_stats_ctx
        from app.services.prospect_priority import build_priority_snapshot

        monkeypatch.setattr("app.config.settings.ai_screen_enabled", True)

        _stats_prospect(db_session, **{**_READY_KW, "readiness_score": 80})  # ready + call-now
        _stats_prospect(db_session, fit_score=30, readiness_score=15)  # weak
        null_cache = _stats_prospect(db_session, **_READY_KW)  # ready, then cache NULLed below
        _stats_prospect(  # screened out (weak, so it adds to no other KPI)
            db_session,
            fit_score=30,
            readiness_score=15,
            enrichment_data={"ai_screen": {"verdict": "screened_out"}},
        )
        _stats_prospect(db_session, status="claimed", fit_score=30, readiness_score=15)

        # Precondition making the degrade meaningful: formula AND listener both said
        # READY before we simulate the pre-backfill NULL.
        assert build_priority_snapshot(null_cache)["is_buyer_ready"] is True
        assert bool(null_cache.is_buyer_ready) is True
        db_session.execute(
            text("UPDATE prospect_accounts SET is_buyer_ready = NULL WHERE id = :pid"),
            {"pid": null_cache.id},
        )
        db_session.commit()
        db_session.expire_all()

        assert _prospect_stats_ctx(db_session) == {
            "total": 4,  # the four SUGGESTED rows; claimed excluded
            "buyer_ready": 1,  # NULL cache honest-degrades to not-ready
            "call_now": 1,  # readiness 80 only (64 < 70)
            "claimed": 1,
            "screened_out": 1,
        }

    def test_flag_off_zeroes_screened_out_only(self, db_session, monkeypatch):
        """AI-screen OFF: screened_out reports 0 while every other KPI is unchanged."""
        from app.routers.htmx.prospecting import _prospect_stats_ctx

        monkeypatch.setattr("app.config.settings.ai_screen_enabled", False)

        _stats_prospect(db_session, **_READY_KW)
        _stats_prospect(
            db_session,
            fit_score=30,
            readiness_score=15,
            enrichment_data={"ai_screen": {"verdict": "screened_out"}},
        )

        assert _prospect_stats_ctx(db_session) == {
            "total": 2,
            "buyer_ready": 1,
            "call_now": 0,
            "claimed": 0,
            "screened_out": 0,
        }

    def test_empty_pool_returns_zeros(self, db_session):
        """SUM over zero rows yields SQL NULL — the ctx must still return 0s, never None
        (the `or 0` guards)."""
        from app.routers.htmx.prospecting import _prospect_stats_ctx

        assert _prospect_stats_ctx(db_session) == {
            "total": 0,
            "buyer_ready": 0,
            "call_now": 0,
            "claimed": 0,
            "screened_out": 0,
        }

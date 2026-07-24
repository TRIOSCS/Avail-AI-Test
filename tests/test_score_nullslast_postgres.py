"""test_score_nullslast_postgres.py — PostgreSQL-only pin: NULL-scored
VendorSightingSummary rows sort LAST, never first, on ``score.desc()`` (finding #F1,
THEME F deep-review #2, re-verification of #26's ``.nullslast()`` additions).

Postgres' ``DESC`` default sorts NULLs FIRST — the opposite of SQLite, where this is
untestable (SQLite's in-memory engine sorts NULLs last on DESC by default), so a
regression that drops ``.nullslast()`` from these three surfaces is invisible to the main
SQLite suite. Runs only against a real Postgres (``PG_TEST_DSN`` set); skipped otherwise.

Called by: pytest (dedicated CI "postgres-paths" job)
Depends on: app.routers.sightings (sightings_list, sightings_detail),
            app.routers.htmx.parts (part_tab_sourcing), tests.conftest (pg_client,
            pg_session, requires_postgres)
"""

from __future__ import annotations

from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.models import User
from app.models.sourcing import Requirement, Requisition
from app.models.vendor_sighting_summary import VendorSightingSummary
from tests.conftest import requires_postgres


def _seed_requirement(pg_session: Session, user: User, mpn: str) -> Requirement:
    req = Requisition(name=f"PG NullsLast {mpn}", status="open", customer_name="Acme", created_by=user.id)
    pg_session.add(req)
    pg_session.flush()
    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        manufacturer="TestMfr",
        target_qty=100,
        sourcing_status="open",
    )
    pg_session.add(requirement)
    pg_session.flush()
    return requirement


def _seed_summaries(pg_session: Session, requirement: Requirement) -> None:
    pg_session.add(
        VendorSightingSummary(
            requirement_id=requirement.id,
            vendor_name="Null Score Vendor",
            estimated_qty=10,
            listing_count=1,
            score=None,
            tier="Poor",
        )
    )
    pg_session.add(
        VendorSightingSummary(
            requirement_id=requirement.id,
            vendor_name="Scored Vendor",
            estimated_qty=200,
            listing_count=2,
            score=75.0,
            tier="Good",
        )
    )
    pg_session.commit()


def _capture_ctx(monkeypatch, module) -> dict:
    captured: dict = {}

    def _capture(template, ctx, *a, **k):
        captured["ctx"] = ctx
        return HTMLResponse("<html/>")

    monkeypatch.setattr(module, "template_response", _capture)
    return captured


@requires_postgres
class TestNullScoreSortsLastOnPostgres:
    def test_board_top_vendor_picks_scored_vendor_not_null(self, pg_client, pg_session: Session, monkeypatch):
        """GET /v2/partials/sightings's per-requirement 'top vendor' pick must be the
        scored vendor — a NULL-scored row sorting first (no ``.nullslast()``) would
        silently promote the un-scored summary as 'top'."""
        from app.routers import sightings as sightings_router

        user = pg_session.query(User).filter_by(email="pgbuyer@trioscs.com").one()
        requirement = _seed_requirement(pg_session, user, "PGNULL-BOARD-1")
        _seed_summaries(pg_session, requirement)
        sightings_router._invalidate_cache("sightings_stat_counts")

        captured = _capture_ctx(monkeypatch, sightings_router)
        resp = pg_client.get("/v2/partials/sightings")
        assert resp.status_code == 200

        top = captured["ctx"]["top_vendors"].get(requirement.id)
        assert top is not None
        assert top["vendor_name"] == "Scored Vendor"

    def test_detail_panel_lists_scored_vendor_before_null(self, pg_client, pg_session: Session, monkeypatch):
        """GET /v2/partials/sightings/{id}/detail renders ``summaries`` in query order —
        the scored vendor must render BEFORE the NULL-scored one."""
        from app.routers import sightings as sightings_router

        user = pg_session.query(User).filter_by(email="pgbuyer@trioscs.com").one()
        requirement = _seed_requirement(pg_session, user, "PGNULL-DETAIL-1")
        _seed_summaries(pg_session, requirement)

        captured = _capture_ctx(monkeypatch, sightings_router)
        resp = pg_client.get(f"/v2/partials/sightings/{requirement.id}/detail")
        assert resp.status_code == 200

        names = [s.vendor_name for s in captured["ctx"]["summaries"]]
        assert names.index("Scored Vendor") < names.index("Null Score Vendor")

    def test_part_tab_sourcing_lists_scored_vendor_before_null(self, pg_client, pg_session: Session, monkeypatch):
        """GET /v2/partials/parts/{id}/tab/sourcing renders ``summaries`` in query order
        too — same NULL-sorts-last requirement."""
        from app.routers.htmx import parts as parts_router

        user = pg_session.query(User).filter_by(email="pgbuyer@trioscs.com").one()
        requirement = _seed_requirement(pg_session, user, "PGNULL-PARTS-1")
        _seed_summaries(pg_session, requirement)

        captured = _capture_ctx(monkeypatch, parts_router)
        resp = pg_client.get(f"/v2/partials/parts/{requirement.id}/tab/sourcing")
        assert resp.status_code == 200

        names = [s.vendor_name for s in captured["ctx"]["summaries"]]
        assert names.index("Scored Vendor") < names.index("Null Score Vendor")

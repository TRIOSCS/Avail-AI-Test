"""test_sightings_mirror_exclusion.py — buyer sightings board excludes the resell
mirror's virtual scratch requisition/requirement (THEME F finding #25).

The resell mirror (app/services/excess_mirror.py) hangs every posted ExcessLineItem's
Sighting off ONE system-owned "Customer Excess (list N)" scratch Requisition +
Requirement per list (is_scratch=True). That virtual requirement must never appear on
the buyer sightings board, its dashboard-strip counters, or the CSV export — it's supply
advertising, not buyer demand a human should source.

Called by: pytest
Depends on: app.routers.sightings.build_board_requirement_query, app.services.excess_mirror,
            app.services.excess_service, tests.conftest
"""

from __future__ import annotations

from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.sourcing import Requirement, Requisition
from app.routers import sightings as sightings_router
from app.routers.sightings import build_board_requirement_query
from app.schemas.sightings import SightingsListParams
from app.services.excess_mirror import ensure_virtual_requirement
from app.services.excess_service import create_excess_list, import_line_items
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


def _make_company(db: Session, name: str = "Excess Seller") -> Company:
    co = Company(name=name)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_real_requirement(db: Session, user: User, mpn: str = "REALPART1") -> Requirement:
    req = Requisition(name="Real Buyer Req", status="open", created_by=user.id)
    db.add(req)
    db.flush()
    requirement = Requirement(requisition_id=req.id, primary_mpn=mpn, sourcing_status="open")
    db.add(requirement)
    db.commit()
    db.refresh(requirement)
    return requirement


def test_board_query_excludes_mirror_virtual_requirement(db_session: Session, test_user: User):
    """build_board_requirement_query (the buyer sightings board's single source of
    truth) drops the mirror's virtual "Customer Excess (list N)" requirement while
    keeping a real buyer requirement."""
    company = _make_company(db_session)
    el = create_excess_list(db_session, title="Excess", company_id=company.id, owner_id=test_user.id)
    import_line_items(db_session, el.id, [{"part_number": "MIRRORPART1", "quantity": "10"}])
    virtual_requirement = ensure_virtual_requirement(db_session, el)
    db_session.commit()

    real_requirement = _make_real_requirement(db_session, test_user)

    ids = {r.id for r in build_board_requirement_query(db_session, test_user, SightingsListParams()).all()}

    assert virtual_requirement.id not in ids, "mirror's virtual requirement leaked onto the board"
    assert real_requirement.id in ids, "real requirement missing from the board"


def test_board_query_excludes_mirror_even_after_publish(db_session: Session, test_user: User):
    """Publishing the list (status draft -> open) still keeps the virtual requirement
    off the board — is_scratch, not list status, is the exclusion signal."""
    from app.services.excess_mirror import publish_list

    company = _make_company(db_session)
    el = create_excess_list(db_session, title="Excess", company_id=company.id, owner_id=test_user.id)
    import_line_items(db_session, el.id, [{"part_number": "MIRRORPART2", "quantity": "5"}])
    publish_list(db_session, el.id, test_user)
    db_session.refresh(el)
    assert el.status == ExcessListStatus.OPEN

    virtual_req = (
        db_session.query(Requisition)
        .filter(Requisition.is_scratch.is_(True), Requisition.name == f"Customer Excess (list {el.id})")
        .one()
    )
    virtual_requirement = db_session.query(Requirement).filter_by(requisition_id=virtual_req.id).one()

    ids = {r.id for r in build_board_requirement_query(db_session, test_user, SightingsListParams()).all()}
    assert virtual_requirement.id not in ids


def test_dashboard_counters_exclude_mirror_virtual_requirement(
    client, db_session: Session, test_user: User, monkeypatch
):
    """The dashboard-strip counters (urgent/stale/pending/unassigned) exclude the
    mirror's virtual scratch requirement — its own unassigned/high-priority shape would
    otherwise inflate the badges shown directly above the (already-filtered) board list
    (finding #25, THEME F)."""
    company = _make_company(db_session)
    el = create_excess_list(db_session, title="Excess", company_id=company.id, owner_id=test_user.id)
    import_line_items(db_session, el.id, [{"part_number": "MIRRORCOUNTS1", "quantity": "10"}])
    virtual_requirement = ensure_virtual_requirement(db_session, el)
    # The mirror's own virtual requirement would (if counted) inflate BOTH the "Urgent"
    # (high priority) and "Unassigned" (no assigned_buyer_id, its natural state) badges.
    virtual_requirement.priority_score = 95
    db_session.commit()

    real_requirement = _make_real_requirement(db_session, test_user, mpn="REALCOUNTS1")
    real_requirement.assigned_buyer_id = test_user.id
    real_requirement.priority_score = 5
    db_session.commit()

    sightings_router._invalidate_cache("sightings_stat_counts")

    captured: dict = {}

    def _capture(template, ctx, *a, **k):
        captured["ctx"] = ctx
        return HTMLResponse("<html/>")

    monkeypatch.setattr(sightings_router, "template_response", _capture)
    resp = client.get("/v2/partials/sightings")
    assert resp.status_code == 200

    counters = captured["ctx"]["dashboard_counters"]
    assert counters["urgent"] == 0, "the mirror's high-priority virtual requirement leaked into 'Urgent'"
    assert counters["unassigned"] == 0, "the mirror's unassigned virtual requirement leaked into 'Unassigned'"


def test_stat_counts_exclude_mirror_virtual_requirement(client, db_session: Session, test_user: User, monkeypatch):
    """The (cached) status-pill facet counts exclude the mirror's virtual scratch
    requirement — both it and a real requirement share ``sourcing_status='open'``, so a
    leaked mirror row would double the 'open' facet count (finding #25, THEME F)."""
    company = _make_company(db_session)
    el = create_excess_list(db_session, title="Excess", company_id=company.id, owner_id=test_user.id)
    import_line_items(db_session, el.id, [{"part_number": "MIRRORCOUNTS2", "quantity": "10"}])
    ensure_virtual_requirement(db_session, el)
    db_session.commit()

    _make_real_requirement(db_session, test_user, mpn="REALCOUNTS2")

    sightings_router._invalidate_cache("sightings_stat_counts")

    captured: dict = {}

    def _capture(template, ctx, *a, **k):
        captured["ctx"] = ctx
        return HTMLResponse("<html/>")

    monkeypatch.setattr(sightings_router, "template_response", _capture)
    resp = client.get("/v2/partials/sightings")
    assert resp.status_code == 200

    # The virtual and the real requirement both carry sourcing_status="open" — a leaked
    # mirror row would read 2 here.
    assert captured["ctx"]["stat_counts"].get("open", 0) == 1

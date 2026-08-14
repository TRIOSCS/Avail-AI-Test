"""test_archive_system.py — Tests for the part-level Archive view (Won/Lost/Hotlist).

Since migration 210 there is no `archived` sourcing status: the Archive view
(`?status=archive`, legacy alias `?status=archived`) is a lens over parts whose
sourcing_status is won/lost/hotlist, across ANY requisition status. Covers the
view filters (including the NULL-status regression and per-status pills), the
bulk-reopen route, bulk view passthrough, and the retirement of the four old
archive routes. There is NO requisition-level archive/hide capability — a
requisition ends in Won or Lost (see test_requisition_state).

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import UTC, datetime

from app.models import Requirement, Requisition
from tests.conftest import engine  # noqa: F401


def _make_requisition(db, user_id, name="REQ-ARCH-001", status="open"):
    """Helper to create a requisition with requirements."""
    req = Requisition(
        name=name,
        customer_name="Test Co",
        status=status,
        created_by=user_id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    return req


def _make_requirement(db, requisition_id, mpn="LM317T", sourcing_status="open", reason=None):
    """Helper to create a requirement."""
    item = Requirement(
        requisition_id=requisition_id,
        primary_mpn=mpn,
        target_qty=1000,
        sourcing_status=sourcing_status,
        outcome_reason=reason,
        created_at=datetime.now(UTC),
    )
    db.add(item)
    db.flush()
    return item


def test_archive_view_shows_won_lost_hotlist(client, db_session, test_user):
    """?status=archive is a lens over won/lost/hotlist parts — open parts stay out."""
    req = _make_requisition(db_session, test_user.id, name="REQ-ARCVIEW")
    _make_requirement(db_session, req.id, mpn="ARC-WON", sourcing_status="won", reason="po received")
    _make_requirement(db_session, req.id, mpn="ARC-LOST", sourcing_status="lost", reason="price")
    _make_requirement(db_session, req.id, mpn="ARC-HOT", sourcing_status="hotlist")
    _make_requirement(db_session, req.id, mpn="ARC-OPEN", sourcing_status="open")
    db_session.commit()

    resp = client.get("/v2/partials/parts?status=archive")
    assert resp.status_code == 200
    assert "ARC-WON" in resp.text
    assert "ARC-LOST" in resp.text
    assert "ARC-HOT" in resp.text
    assert "ARC-OPEN" not in resp.text


def test_legacy_archived_alias_still_works(client, db_session, test_user):
    """?status=archived (old bookmark spelling) behaves exactly like ?status=archive."""
    req = _make_requisition(db_session, test_user.id, name="REQ-ALIAS")
    _make_requirement(db_session, req.id, mpn="ALIAS-LOST", sourcing_status="lost", reason="nla")
    _make_requirement(db_session, req.id, mpn="ALIAS-OPEN", sourcing_status="open")
    db_session.commit()

    resp = client.get("/v2/partials/parts?status=archived")
    assert resp.status_code == 200
    assert "ALIAS-LOST" in resp.text
    assert "ALIAS-OPEN" not in resp.text


def test_archive_view_ignores_requisition_status(client, db_session, test_user):
    """Archive-view statuses are findable on ANY requisition status (won deals too)."""
    won_req = _make_requisition(db_session, test_user.id, name="REQ-WONDEAL", status="won")
    _make_requirement(db_session, won_req.id, mpn="WONDEAL-HOT", sourcing_status="hotlist")
    db_session.commit()

    resp = client.get("/v2/partials/parts?status=archive")
    assert resp.status_code == 200
    assert "WONDEAL-HOT" in resp.text

    # The dedicated hotlist pill finds it too.
    resp = client.get("/v2/partials/parts?status=hotlist")
    assert resp.status_code == 200
    assert "WONDEAL-HOT" in resp.text


def test_status_pill_won_returns_only_won(client, db_session, test_user):
    """?status=won returns won parts only (no requisition-status filter)."""
    req = _make_requisition(db_session, test_user.id, name="REQ-WONPILL", status="lost")
    _make_requirement(db_session, req.id, mpn="PILL-WON", sourcing_status="won", reason="po")
    _make_requirement(db_session, req.id, mpn="PILL-HOT", sourcing_status="hotlist")
    db_session.commit()

    resp = client.get("/v2/partials/parts?status=won")
    assert resp.status_code == 200
    assert "PILL-WON" in resp.text
    assert "PILL-HOT" not in resp.text


def test_default_view_excludes_archive_statuses(client, db_session, test_user):
    """The default list hides won/lost/hotlist parts and keeps NULL-status parts.

    NULL regression guard: sourcing_status is nullable (client-side default
    only) — a bare `notin_` would silently drop NULL rows.
    """
    req = _make_requisition(db_session, test_user.id, name="REQ-DEFAULT")
    _make_requirement(db_session, req.id, mpn="DEF-OPEN", sourcing_status="open")
    _make_requirement(db_session, req.id, mpn="DEF-NULL", sourcing_status=None)
    _make_requirement(db_session, req.id, mpn="DEF-WON", sourcing_status="won", reason="po")
    _make_requirement(db_session, req.id, mpn="DEF-HOT", sourcing_status="hotlist")
    db_session.commit()

    resp = client.get("/v2/partials/parts")
    assert resp.status_code == 200
    assert "DEF-OPEN" in resp.text
    assert "DEF-NULL" in resp.text
    assert "DEF-WON" not in resp.text
    assert "DEF-HOT" not in resp.text


def test_bulk_reopen(client, db_session, test_user):
    """POST bulk-reopen moves won/lost/hotlist parts back to open and clears the
    reason."""
    req = _make_requisition(db_session, test_user.id, name="REQ-REOPEN")
    p_won = _make_requirement(db_session, req.id, mpn="RO-WON", sourcing_status="won", reason="po")
    p_lost = _make_requirement(db_session, req.id, mpn="RO-LOST", sourcing_status="lost", reason="price")
    p_hot = _make_requirement(db_session, req.id, mpn="RO-HOT", sourcing_status="hotlist")
    db_session.commit()

    resp = client.post(
        "/v2/partials/parts/bulk-reopen",
        json={"requirement_ids": [p_won.id, p_lost.id, p_hot.id]},
    )
    assert resp.status_code == 200
    assert "3 part(s) reopened" in resp.headers.get("HX-Trigger", "")

    for p in (p_won, p_lost, p_hot):
        db_session.refresh(p)
        assert p.sourcing_status == "open"
        assert p.outcome_reason is None


def test_bulk_reopen_skips_noop(client, db_session, test_user):
    """Reopening an already-open part is a no-op reported as skipped-safe (count 0)."""
    req = _make_requisition(db_session, test_user.id, name="REQ-REOPEN-NOOP")
    p_open = _make_requirement(db_session, req.id, mpn="RO-OPEN", sourcing_status="open")
    db_session.commit()

    resp = client.post("/v2/partials/parts/bulk-reopen", json={"requirement_ids": [p_open.id]})
    assert resp.status_code == 200
    assert "0 part(s) reopened" in resp.headers.get("HX-Trigger", "")


def test_bulk_view_passthrough_keeps_archive_view(client, db_session, test_user):
    """A bulk action from the Arc view re-renders the ARCHIVE list, not the default
    one."""
    req = _make_requisition(db_session, test_user.id, name="REQ-PASSTHRU")
    p_hot = _make_requirement(db_session, req.id, mpn="PT-HOT", sourcing_status="hotlist")
    _make_requirement(db_session, req.id, mpn="PT-LOST", sourcing_status="lost", reason="nla")
    _make_requirement(db_session, req.id, mpn="PT-OPEN", sourcing_status="open")
    db_session.commit()

    resp = client.post(
        "/v2/partials/parts/bulk-outcome",
        json={"requirement_ids": [p_hot.id], "outcome": "lost", "reason": "gone", "status": "archive"},
    )
    assert resp.status_code == 200
    # Response is the archive view: the still-lost sibling shows, open part doesn't.
    assert "PT-LOST" in resp.text
    assert "PT-OPEN" not in resp.text


def test_old_archive_routes_are_retired(client, db_session, test_user):
    """The four pre-210 archive routes are gone (404/405)."""
    req = _make_requisition(db_session, test_user.id, name="REQ-RETIRED")
    part = _make_requirement(db_session, req.id, mpn="RETIRED-PART", sourcing_status="open")
    db_session.commit()

    assert client.patch(f"/v2/partials/parts/{part.id}/archive").status_code in (404, 405)
    assert client.patch(f"/v2/partials/parts/{part.id}/unarchive").status_code in (404, 405)
    assert client.post("/v2/partials/parts/bulk-archive", json={"requirement_ids": [part.id]}).status_code in (404, 405)
    assert client.post("/v2/partials/parts/bulk-unarchive", json={"requirement_ids": [part.id]}).status_code in (
        404,
        405,
    )

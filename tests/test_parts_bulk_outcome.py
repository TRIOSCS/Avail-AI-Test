"""test_parts_bulk_outcome.py — Sales-Hub per-part Mark Won / Mark Lost bulk action.

Exercises POST /v2/partials/parts/bulk-outcome (the replacement for the removed
bulk Archive): required shared reason, per-part SourcingStatus.WON/LOST transition,
skip-illegal-state, bad-outcome / blank-reason 400s, and auth parity with
bulk_archive. Plus a template render assertion that the bulk bar now shows
Mark Won / Mark Lost and no Archive button.

Depends on: conftest fixtures (client, db_session, test_user, test_requisition).
"""

import inspect

from fastapi.params import Depends as DependsParam

from app.constants import SourcingStatus
from app.models import Requirement, Requisition

BULK_OUTCOME_URL = "/v2/partials/parts/bulk-outcome"


def _make_requirement(db, req_id, mpn, status=SourcingStatus.OPEN):
    r = Requirement(requisition_id=req_id, primary_mpn=mpn, sourcing_status=status)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _make_requisition(db, user, name="REQ-OUTCOME"):
    req = Requisition(name=name, customer_name="Acme", status="open", created_by=user.id)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def test_mark_won_sets_status_and_reason(client, db_session, test_user):
    req = _make_requisition(db_session, test_user)
    part = _make_requirement(db_session, req.id, "PART-WON")

    resp = client.post(
        BULK_OUTCOME_URL,
        json={"requirement_ids": [part.id], "outcome": "won", "reason": "Customer PO received"},
    )

    assert resp.status_code == 200
    assert "showToast" in resp.headers.get("HX-Trigger", "")
    refreshed = db_session.get(Requirement, part.id)
    assert refreshed.sourcing_status == SourcingStatus.WON
    assert refreshed.outcome_reason == "Customer PO received"


def test_mark_lost_sets_status_and_reason(client, db_session, test_user):
    req = _make_requisition(db_session, test_user)
    part = _make_requirement(db_session, req.id, "PART-LOST")

    resp = client.post(
        BULK_OUTCOME_URL,
        json={"requirement_ids": [part.id], "outcome": "lost", "reason": "Lost to competitor"},
    )

    assert resp.status_code == 200
    refreshed = db_session.get(Requirement, part.id)
    assert refreshed.sourcing_status == SourcingStatus.LOST
    assert refreshed.outcome_reason == "Lost to competitor"


def test_blank_reason_rejected_400(client, db_session, test_user):
    req = _make_requisition(db_session, test_user)
    part = _make_requirement(db_session, req.id, "PART-BLANK")

    resp = client.post(
        BULK_OUTCOME_URL,
        json={"requirement_ids": [part.id], "outcome": "won", "reason": "   "},
    )

    assert resp.status_code == 400
    assert "error" in resp.json()
    # Nothing changed.
    assert db_session.get(Requirement, part.id).sourcing_status == SourcingStatus.OPEN


def test_missing_reason_rejected_400(client, db_session, test_user):
    req = _make_requisition(db_session, test_user)
    part = _make_requirement(db_session, req.id, "PART-NOREASON")

    resp = client.post(BULK_OUTCOME_URL, json={"requirement_ids": [part.id], "outcome": "won"})

    assert resp.status_code == 400
    assert db_session.get(Requirement, part.id).sourcing_status == SourcingStatus.OPEN


def test_bad_outcome_rejected_400(client, db_session, test_user):
    req = _make_requisition(db_session, test_user)
    part = _make_requirement(db_session, req.id, "PART-BADOUT")

    resp = client.post(
        BULK_OUTCOME_URL,
        json={"requirement_ids": [part.id], "outcome": "maybe", "reason": "whatever"},
    )

    assert resp.status_code == 400
    assert db_session.get(Requirement, part.id).sourcing_status == SourcingStatus.OPEN


def test_multiple_ids_one_call(client, db_session, test_user):
    req = _make_requisition(db_session, test_user)
    p1 = _make_requirement(db_session, req.id, "PART-M1")
    p2 = _make_requirement(db_session, req.id, "PART-M2", status=SourcingStatus.QUOTED)

    resp = client.post(
        BULK_OUTCOME_URL,
        json={"requirement_ids": [p1.id, p2.id], "outcome": "won", "reason": "Combined order"},
    )

    assert resp.status_code == 200
    assert "2 part" in resp.headers.get("HX-Trigger", "")
    assert db_session.get(Requirement, p1.id).sourcing_status == SourcingStatus.WON
    assert db_session.get(Requirement, p2.id).sourcing_status == SourcingStatus.WON
    assert db_session.get(Requirement, p1.id).outcome_reason == "Combined order"
    assert db_session.get(Requirement, p2.id).outcome_reason == "Combined order"


def test_illegal_state_skipped_not_500(client, db_session, test_user):
    """An archived part (archived -> won is illegal) is skipped; open part still
    wins."""
    req = _make_requisition(db_session, test_user)
    ok = _make_requirement(db_session, req.id, "PART-OK")
    archived = _make_requirement(db_session, req.id, "PART-ARC", status=SourcingStatus.ARCHIVED)

    resp = client.post(
        BULK_OUTCOME_URL,
        json={"requirement_ids": [ok.id, archived.id], "outcome": "won", "reason": "Won it"},
    )

    assert resp.status_code == 200
    assert "1 part" in resp.headers.get("HX-Trigger", "")
    assert db_session.get(Requirement, ok.id).sourcing_status == SourcingStatus.WON
    # Archived one untouched — no partial write, no crash.
    assert db_session.get(Requirement, archived.id).sourcing_status == SourcingStatus.ARCHIVED
    assert db_session.get(Requirement, archived.id).outcome_reason is None


def _auth_deps(fn):
    """Set of dependency callables declared via Depends(...) in a route signature."""
    return {
        p.default.dependency for p in inspect.signature(fn).parameters.values() if isinstance(p.default, DependsParam)
    }


def test_auth_parity_with_bulk_archive():
    """Bulk-outcome must use the exact same auth/dependency set as bulk_archive."""
    from app.dependencies import require_user
    from app.routers.htmx.parts import bulk_archive, bulk_outcome

    outcome_deps = _auth_deps(bulk_outcome)
    archive_deps = _auth_deps(bulk_archive)
    assert require_user in outcome_deps
    assert outcome_deps == archive_deps


def test_bulk_bar_shows_mark_won_lost_no_archive(client, db_session, test_user):
    req = _make_requisition(db_session, test_user)
    _make_requirement(db_session, req.id, "PART-RENDER")

    resp = client.get("/v2/partials/parts")

    assert resp.status_code == 200
    html = resp.text
    assert "Mark Won" in html
    assert "Mark Lost" in html
    # The old bulk Archive action + its JS handler are gone.
    assert "bulkArchive" not in html

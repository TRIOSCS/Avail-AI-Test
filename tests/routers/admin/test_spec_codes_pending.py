"""Smoke tests for the pending OEM spec-code approval admin router.

Covers: list page render, approve (with and without buyer-edited AVL), reject
(MPNs default to all proposed when rejected_mpns is empty), and reason
validation. Uses a local admin TestClient fixture matching the pattern from
tests/test_routers_admin.py.

Called by: pytest
Depends on: app/routers/admin/spec_codes.py, conftest.py (db_session, admin_user)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.sourcing import (
    OemSpecCode,
    OemSpecCodeBlacklist,
    OemSpecCodePending,
)

# ── Test client fixture ───────────────────────────────────────────────


@pytest.fixture()
def client_with_settings_user(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with overrides for settings-access (admin) auth."""
    from app.database import get_db
    from app.dependencies import require_admin, require_settings_access, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_admin] = _override_admin
    app.dependency_overrides[require_settings_access] = _override_admin
    app.dependency_overrides[require_user] = _override_admin

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in [get_db, require_admin, require_settings_access, require_user]:
            app.dependency_overrides.pop(dep, None)


# ── Pending row fixture ───────────────────────────────────────────────


@pytest.fixture()
def pending_row(db_session: Session) -> OemSpecCodePending:
    row = OemSpecCodePending(
        oem="IBM",
        spec_code="SPREJ",
        proposed_avl=[
            {
                "mpn": "GRM188R71H103KA01D",
                "manufacturer": "Murata",
                "rank": 1,
                "notes": None,
            }
        ],
        llm_confidence=0.8,
        citations=[{"url": "https://example.com", "snippet": "datasheet"}],
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


# ── Tests ─────────────────────────────────────────────────────────────


def test_list_pending_returns_200(client_with_settings_user, pending_row):
    resp = client_with_settings_user.get("/admin/spec-codes/pending")
    assert resp.status_code == 200
    assert b"SPREJ" in resp.content


def test_approve_promotes_to_oem_spec_codes(client_with_settings_user, pending_row, db_session, admin_user):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/approve",
        json={"edited_avl": None},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(OemSpecCodePending).count() == 0
    promoted = db_session.query(OemSpecCode).filter_by(oem="IBM", spec_code="SPREJ").one()
    assert promoted.source == "llm_approved"
    assert promoted.approved_by_user_id == admin_user.id
    assert promoted.approved_at is not None
    assert promoted.avl[0]["mpn"] == "GRM188R71H103KA01D"


def test_approve_with_edited_avl_uses_edited(client_with_settings_user, pending_row, db_session):
    edited = [
        {
            "mpn": "CORRECTED_MPN",
            "manufacturer": "Murata",
            "rank": 1,
            "notes": "corrected by buyer",
        }
    ]
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/approve",
        json={"edited_avl": edited},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    promoted = db_session.query(OemSpecCode).filter_by(oem="IBM", spec_code="SPREJ").one()
    assert promoted.avl[0]["mpn"] == "CORRECTED_MPN"
    assert promoted.avl[0]["notes"] == "corrected by buyer"


def test_reject_moves_mpns_to_blacklist(client_with_settings_user, pending_row, db_session, admin_user):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/reject",
        json={"reason": "wrong package", "rejected_mpns": []},
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(OemSpecCodePending).count() == 0
    bl = db_session.query(OemSpecCodeBlacklist).filter_by(oem="IBM", spec_code="SPREJ").one()
    assert "GRM188R71H103KA01D" in bl.rejected_mpns
    assert bl.reason == "wrong package"
    assert bl.rejected_by_user_id == admin_user.id


def test_reject_requires_reason(client_with_settings_user, pending_row):
    resp = client_with_settings_user.post(
        f"/admin/spec-codes/pending/{pending_row.id}/reject",
        json={"rejected_mpns": []},  # no reason field
    )
    assert resp.status_code == 422

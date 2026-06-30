"""Read-IDOR regression for requisition/requirement GET partials.

The canonical v2 path scopes requisition reads by ownership, but these legacy
GET partials/APIs loaded the record by id and only 404'd on missing — never
calling require_requisition_access — so a restricted (SALES/TRADER) non-owner
could read another rep's requirement offers/history, requisition detail, and
tabs by id. A restricted non-owner must get 404 (existence not leaked); owners
and unrestricted buyers must still get 200.

Called by: pytest
Depends on: app.routers.htmx.requisitions, app.routers.requisitions.requirements,
            conftest fixtures (client, db_session, test_requisition, test_user, admin_user)
"""

from app.constants import UserRole
from app.models import Requirement


def _rid(db_session, test_requisition):
    return db_session.query(Requirement).filter(Requirement.requisition_id == test_requisition.id).first().id


def _make_foreign(db_session, test_requisition, test_user, admin_user, role=UserRole.SALES):
    """Restrict test_user and hand requisition ownership to someone else."""
    test_user.role = role
    test_requisition.created_by = admin_user.id
    db_session.commit()


# ── GET /v2/partials/requisitions/{req_id} (detail) ──────────────────────────


def test_requisition_detail_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user)
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}").status_code == 404


def test_requisition_detail_blocks_non_owner_trader(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user, role=UserRole.TRADER)
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}").status_code == 404


def test_requisition_detail_allows_owning_sales(client, db_session, test_requisition, test_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    db_session.commit()
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}").status_code == 200


# ── GET /v2/partials/requisitions/{req_id}/tab/{tab} ─────────────────────────


def test_requisition_tab_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user)
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/parts").status_code == 404


def test_requisition_tab_allows_buyer(client, db_session, test_requisition, test_user):
    assert test_user.role == "buyer"
    assert client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/parts").status_code == 200


# ── GET /api/requirements/{requirement_id}/offers ────────────────────────────


def test_requirement_offers_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user)
    rid = _rid(db_session, test_requisition)
    assert client.get(f"/api/requirements/{rid}/offers").status_code == 404


def test_requirement_offers_allows_buyer(client, db_session, test_requisition, test_user):
    rid = _rid(db_session, test_requisition)
    assert client.get(f"/api/requirements/{rid}/offers").status_code == 200


# ── GET /api/requirements/{requirement_id}/history ───────────────────────────


def test_requirement_history_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user)
    rid = _rid(db_session, test_requisition)
    assert client.get(f"/api/requirements/{rid}/history").status_code == 404


def test_requirement_history_allows_buyer(client, db_session, test_requisition, test_user):
    rid = _rid(db_session, test_requisition)
    assert client.get(f"/api/requirements/{rid}/history").status_code == 200

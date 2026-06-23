"""Authz regression tests for app/routers/requisitions/core.py.

Every mutating endpoint in core.py loads its requisition via
``get_req_for_user(db, user, req_id)``, which filters by ``created_by`` for
RESTRICTED_ROLES (SALES + TRADER). These tests pin that behavior: a restricted
non-owner must get 404 (existence not leaked) on requisition-scoped mutations,
while the buyer happy path keeps working.
"""

from app.constants import UserRole


def _make_foreign(test_requisition, test_user, owner, db_session, role=UserRole.SALES):
    """Flip the acting user to a restricted role and reassign the req to someone
    else."""
    test_user.role = role
    test_requisition.created_by = owner.id
    db_session.commit()


# ── PUT /api/requisitions/{req_id}/outcome ────────────────────────────────


def test_outcome_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(test_requisition, test_user, admin_user, db_session)
    resp = client.put(f"/api/requisitions/{test_requisition.id}/outcome", json={"outcome": "won"})
    assert resp.status_code == 404


def test_outcome_blocks_non_owner_trader(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(test_requisition, test_user, admin_user, db_session, role=UserRole.TRADER)
    resp = client.put(f"/api/requisitions/{test_requisition.id}/outcome", json={"outcome": "won"})
    assert resp.status_code == 404


def test_outcome_buyer_happy_path(client, test_requisition):
    resp = client.put(f"/api/requisitions/{test_requisition.id}/outcome", json={"outcome": "won"})
    assert resp.status_code == 200


# ── PUT /api/requisitions/{req_id}/archive ────────────────────────────────


def test_archive_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(test_requisition, test_user, admin_user, db_session)
    resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
    assert resp.status_code == 404


def test_archive_buyer_happy_path(client, test_requisition):
    resp = client.put(f"/api/requisitions/{test_requisition.id}/archive")
    assert resp.status_code == 200


# ── PUT /api/requisitions/{req_id} (update) ───────────────────────────────


def test_update_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(test_requisition, test_user, admin_user, db_session)
    resp = client.put(f"/api/requisitions/{test_requisition.id}", json={"name": "Hacked"})
    assert resp.status_code == 404


def test_update_buyer_happy_path(client, test_requisition):
    resp = client.put(f"/api/requisitions/{test_requisition.id}", json={"name": "Renamed"})
    assert resp.status_code == 200


# ── POST /api/requisitions/{req_id}/dismiss-new-offers ────────────────────


def test_dismiss_new_offers_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(test_requisition, test_user, admin_user, db_session)
    resp = client.post(f"/api/requisitions/{test_requisition.id}/dismiss-new-offers")
    assert resp.status_code == 404


def test_dismiss_new_offers_buyer_happy_path(client, test_requisition):
    resp = client.post(f"/api/requisitions/{test_requisition.id}/dismiss-new-offers")
    assert resp.status_code == 200


# ── DELETE /api/requisitions/{req_id}/claim ───────────────────────────────
# Restricted non-owner must not be able to resolve the req at all (404).


def test_unclaim_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(test_requisition, test_user, admin_user, db_session)
    resp = client.delete(f"/api/requisitions/{test_requisition.id}/claim")
    assert resp.status_code == 404

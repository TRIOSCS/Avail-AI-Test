"""Read-IDOR regression for offers.py requisition-scoped GET partials.

Five GET partial handlers in app.routers.htmx.offers loaded the requisition by
id (get_requisition_or_404) but skipped require_requisition_access — so a
restricted (SALES/TRADER) non-owner could read another rep's requisition name,
customer, MPNs, and vendor contacts by crafting a direct GET. Their mutating
siblings in the same file all call require_requisition_access. A restricted
non-owner must now get 404 (existence not leaked); owners and unrestricted
buyers must still get 200.

Called by: pytest
Depends on: app.routers.htmx.offers, conftest fixtures
            (client, db_session, test_requisition, test_user, admin_user)
"""

import pytest

from app.constants import UserRole

# GET partials that must enforce require_requisition_access.
PARTIAL_PATHS = [
    "parse-email-form",
    "paste-offer-form",
    "add-offer-form",
    "rfq-compose",
    "rfq-prepare",
]


def _make_foreign(db_session, test_requisition, test_user, admin_user, role=UserRole.SALES):
    """Restrict test_user and hand requisition ownership to someone else."""
    test_user.role = role
    test_requisition.created_by = admin_user.id
    db_session.commit()


@pytest.mark.parametrize("suffix", PARTIAL_PATHS)
def test_partial_blocks_non_owner_sales(suffix, client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user)
    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/{suffix}")
    assert resp.status_code == 404


@pytest.mark.parametrize("suffix", PARTIAL_PATHS)
def test_partial_blocks_non_owner_trader(suffix, client, db_session, test_requisition, test_user, admin_user):
    _make_foreign(db_session, test_requisition, test_user, admin_user, role=UserRole.TRADER)
    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/{suffix}")
    assert resp.status_code == 404


@pytest.mark.parametrize("suffix", PARTIAL_PATHS)
def test_partial_allows_owning_sales(suffix, client, db_session, test_requisition, test_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id
    db_session.commit()
    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/{suffix}")
    assert resp.status_code == 200


@pytest.mark.parametrize("suffix", PARTIAL_PATHS)
def test_partial_allows_buyer(suffix, client, db_session, test_requisition, test_user):
    assert test_user.role == "buyer"
    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/{suffix}")
    assert resp.status_code == 200

"""Requisition list-partial ownership scoping for restricted roles.

RESTRICTED_ROLES = {SALES, TRADER}, but the htmx requisitions_list_partial scoped
only SALES — a TRADER saw every requisition's name/customer in the list (while the
now-gated detail/tabs 404 on the same reqs). A restricted role must see only its own;
buyer/manager/admin remain unrestricted.

Called by: pytest
Depends on: app.routers.htmx.requisitions, conftest (client, db_session, test_user, admin_user)
"""

from app.constants import UserRole
from app.models import Requisition


def _req(db, owner_id, name):
    r = Requisition(name=name, status="open", urgency="normal", customer_name="Cust", created_by=owner_id)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def test_trader_sees_only_own_requisitions_in_list(client, db_session, test_user, admin_user):
    _req(db_session, test_user.id, "MINE-REQ-T")
    _req(db_session, admin_user.id, "FOREIGN-REQ-T")
    test_user.role = UserRole.TRADER
    db_session.commit()
    resp = client.get("/v2/partials/requisitions")
    assert resp.status_code == 200
    assert "MINE-REQ-T" in resp.text
    assert "FOREIGN-REQ-T" not in resp.text  # restricted: foreign requisition hidden


def test_buyer_sees_all_requisitions_in_list(client, db_session, test_user, admin_user):
    _req(db_session, test_user.id, "MINE-REQ-B")
    _req(db_session, admin_user.id, "FOREIGN-REQ-B")
    assert test_user.role == "buyer"
    resp = client.get("/v2/partials/requisitions")
    assert "FOREIGN-REQ-B" in resp.text  # unrestricted: sees all

"""Requisition-ownership IDOR regression tests for app/routers/crm/quotes.py.

Every mutating/sending quote endpoint loads its target via the ownership-aware
central helpers (get_quote_for_user / get_req_for_user), which restrict SALES and
TRADER users to resources whose requisition was created_by themselves. These tests
lock in that a restricted (SALES) non-owner is rejected with 404 (existence not
leaked) on each mutating endpoint, while leaving the buyer happy path unchanged.

The shared ``client`` fixture overrides require_user to return ``test_user``; we
flip that user's role to SALES and reassign requisition/quote ownership to a
different user (admin_user) to simulate a restricted non-owner.
"""

from datetime import datetime, timezone

import pytest

from app.constants import QuoteStatus, UserRole
from app.models import CustomerSite, Quote


@pytest.fixture()
def other_owned_quote(db_session, test_requisition, admin_user, test_company):
    """A DRAFT quote whose requisition is owned by admin_user (not test_user)."""
    test_requisition.created_by = admin_user.id

    site = CustomerSite(
        company_id=test_company.id,
        site_name="HQ",
        contact_email="buyer@example.com",
        contact_name="Buyer Person",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(site)
    db_session.flush()
    test_requisition.customer_site_id = site.id

    quote = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=site.id,
        quote_number="Q-AUTHZ-001",
        line_items=[],
        status=QuoteStatus.DRAFT.value,
        created_by_id=admin_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(quote)
    db_session.commit()
    db_session.refresh(quote)
    return quote


def _make_sales(test_user, db_session):
    test_user.role = UserRole.SALES
    db_session.commit()


# ── Non-owner SALES is blocked (404) on every mutating endpoint ──────────


def test_create_quote_blocks_non_owner_sales(client, db_session, test_requisition, test_user, admin_user):
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id
    db_session.commit()
    resp = client.post(
        f"/api/requisitions/{test_requisition.id}/quote",
        json={"offer_ids": [], "line_items": []},
    )
    assert resp.status_code == 404


def test_update_quote_blocks_non_owner_sales(client, db_session, test_user, other_owned_quote):
    _make_sales(test_user, db_session)
    resp = client.put(f"/api/quotes/{other_owned_quote.id}", json={"notes": "hacked"})
    assert resp.status_code == 404


def test_delete_quote_blocks_non_owner_sales(client, db_session, test_user, other_owned_quote):
    _make_sales(test_user, db_session)
    resp = client.delete(f"/api/quotes/{other_owned_quote.id}")
    assert resp.status_code == 404


def test_send_quote_blocks_non_owner_sales(client, db_session, test_user, other_owned_quote):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/quotes/{other_owned_quote.id}/send", json={})
    assert resp.status_code == 404


def test_preview_quote_blocks_non_owner_sales(client, db_session, test_user, other_owned_quote):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/quotes/{other_owned_quote.id}/preview", json={})
    assert resp.status_code == 404


def test_quote_result_blocks_non_owner_sales(client, db_session, test_user, other_owned_quote):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/quotes/{other_owned_quote.id}/result", json={"result": "won"})
    assert resp.status_code == 404


def test_revise_quote_blocks_non_owner_sales(client, db_session, test_user, other_owned_quote):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/quotes/{other_owned_quote.id}/revise")
    assert resp.status_code == 404


def test_reopen_quote_blocks_non_owner_sales(client, db_session, test_user, other_owned_quote):
    _make_sales(test_user, db_session)
    resp = client.post(f"/api/quotes/{other_owned_quote.id}/reopen", json={"revise": False})
    assert resp.status_code == 404


# ── Buyer happy path stays unchanged (owner / unrestricted role allowed) ─


def test_update_quote_allows_buyer_owner(client, other_owned_quote):
    # test_user is a buyer (unrestricted) by default — must NOT be blocked.
    resp = client.put(f"/api/quotes/{other_owned_quote.id}", json={"notes": "ok"})
    assert resp.status_code == 200

"""Authz regression tests for app/routers/quote_builder.py requisition-ownership IDOR.

Policy (approved 2026-06-23): SALES + TRADER may only act on requisitions they
created (created_by). BUYER / MANAGER / ADMIN are unrestricted. Verifies that the
only mutating endpoint in this router — POST .../quote-builder/{req_id}/save —
returns 404 for a restricted non-owner and keeps the buyer happy path working.

The `client` fixture overrides require_user to return `test_user`; flipping that
user's role and re-pointing the requisition's created_by exercises the guard.
"""

from app.constants import UserRole


def _save_payload():
    return {
        "lines": [
            {
                "requirement_id": 1,
                "mpn": "LM317T",
                "manufacturer": "TI",
                "qty": 1000,
                "cost_price": 0.40,
                "sell_price": 0.50,
                "margin_pct": 20.0,
            }
        ]
    }


def test_quote_save_blocks_non_owner_sales(
    client, db_session, test_requisition, test_user, admin_user, test_customer_site
):
    """SALES non-owner is blocked (404) from saving a quote for another's req."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = admin_user.id  # owned by someone else
    test_requisition.customer_site_id = test_customer_site.id
    db_session.commit()

    resp = client.post(
        f"/v2/partials/quote-builder/{test_requisition.id}/save",
        json=_save_payload(),
    )
    assert resp.status_code == 404


def test_quote_save_blocks_non_owner_trader(
    client, db_session, test_requisition, test_user, admin_user, test_customer_site
):
    """TRADER non-owner is blocked (404) — TRADER is restricted just like SALES."""
    test_user.role = UserRole.TRADER
    test_requisition.created_by = admin_user.id
    test_requisition.customer_site_id = test_customer_site.id
    db_session.commit()

    resp = client.post(
        f"/v2/partials/quote-builder/{test_requisition.id}/save",
        json=_save_payload(),
    )
    assert resp.status_code == 404


def test_quote_save_allows_owner_sales(client, db_session, test_requisition, test_user, test_customer_site):
    """SALES owner passes the ownership guard (not blocked by 404)."""
    test_user.role = UserRole.SALES
    test_requisition.created_by = test_user.id  # owner
    test_requisition.customer_site_id = test_customer_site.id
    db_session.commit()

    resp = client.post(
        f"/v2/partials/quote-builder/{test_requisition.id}/save",
        json=_save_payload(),
    )
    # Ownership guard must NOT 404 the owner. (Service may 200 or error on data
    # specifics, but it must not be the ownership 404.)
    assert resp.status_code != 404


def test_quote_save_allows_buyer_non_owner(
    client, db_session, test_requisition, test_user, admin_user, test_customer_site
):
    """BUYER (default role) is unrestricted even when not the owner."""
    # test_user is buyer by default
    test_requisition.created_by = admin_user.id  # someone else's req
    test_requisition.customer_site_id = test_customer_site.id
    db_session.commit()

    resp = client.post(
        f"/v2/partials/quote-builder/{test_requisition.id}/save",
        json=_save_payload(),
    )
    assert resp.status_code != 404

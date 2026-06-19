"""test_quote_idor.py — IDOR ownership-scope sweep for quote-by-id routes.

Verifies that the unscoped quote-by-id loads in app/routers/htmx_views.py and
app/routers/quote_builder.py now route through get_quote_for_user, so a SALES
user cannot reach another SALES user's quote (cross-requisition ownership).

get_quote_for_user scopes by Requisition.created_by == user.id for the SALES
role; ADMIN/BUYER/etc. see all quotes (no behaviour change).

Called by: pytest autodiscovery
Depends on: tests/conftest.py fixtures, app.main app
"""

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.models import CustomerSite, Offer, Quote, QuoteLine, Requisition, User


@contextmanager
def _client_as(db_session, user: User):
    """Yield a TestClient whose require_user resolves to *user*.

    Overrides only the auth/db deps (not require_buyer/require_admin) so the
    in-route role check on `user.role` is exercised exactly as in production.
    """
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


def _make_sales_user(db_session, email: str, azure_id: str) -> User:
    user = User(
        email=email,
        name=email.split("@")[0],
        role="sales",
        azure_id=azure_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_req_with_quote(db_session, owner: User, site: CustomerSite, tag: str) -> tuple[Requisition, Quote]:
    """Create a requisition owned by *owner* plus a draft quote tied to it."""
    req = Requisition(
        name=f"REQ-IDOR-{tag}",
        customer_name="Acme Electronics",
        status="active",
        customer_site_id=site.id,
        created_by=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    quote = Quote(
        requisition_id=req.id,
        customer_site_id=site.id,
        quote_number=f"IDOR-Q-{tag}",
        status="draft",
        line_items=[],
        subtotal=0.0,
        total_cost=0.0,
        total_margin_pct=0.0,
        created_by_id=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(quote)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(quote)
    return req, quote


def test_sales_cannot_reach_other_sales_users_quote(db_session, test_customer_site):
    """SALES user A gets 404 hitting SALES user B's quote on representative routes."""
    user_a = _make_sales_user(db_session, "idor-a@trioscs.com", "azure-idor-a")
    user_b = _make_sales_user(db_session, "idor-b@trioscs.com", "azure-idor-b")

    req_a, quote_a = _make_req_with_quote(db_session, user_a, test_customer_site, "A")
    req_b, quote_b = _make_req_with_quote(db_session, user_b, test_customer_site, "B")

    # An offer on B's requisition, so add-offer would otherwise succeed.
    offer_b = Offer(
        requisition_id=req_b.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=user_b.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer_b)
    db_session.commit()
    db_session.refresh(offer_b)

    with _client_as(db_session, user_a) as c:
        # add_offer_to_quote — quote owned by B
        resp_add_offer = c.post(f"/v2/partials/quotes/{quote_b.id}/add-offer/{offer_b.id}")
        assert resp_add_offer.status_code == 404, resp_add_offer.text

        # edit_quote_metadata — quote owned by B
        resp_edit = c.post(
            f"/v2/partials/quotes/{quote_b.id}/edit",
            data={"notes": "should not apply"},
        )
        assert resp_edit.status_code == 404, resp_edit.text

        # delete_quote_htmx — quote owned by B
        resp_delete = c.delete(f"/v2/partials/quotes/{quote_b.id}")
        assert resp_delete.status_code == 404, resp_delete.text

        # quote_builder export — req_id/quote_id both B's
        resp_export = c.get(f"/v2/partials/quote-builder/{req_b.id}/export/excel?quote_id={quote_b.id}")
        assert resp_export.status_code == 404, resp_export.text

    # B's quote must be untouched by A's attempts.
    db_session.refresh(quote_b)
    assert quote_b.status == "draft"
    assert quote_b.notes is None or quote_b.notes != "should not apply"


def test_sales_can_reach_own_quote(db_session, test_customer_site):
    """SALES user A can edit their OWN quote (positive control — not a 404)."""
    user_a = _make_sales_user(db_session, "idor-own@trioscs.com", "azure-idor-own")
    _req_a, quote_a = _make_req_with_quote(db_session, user_a, test_customer_site, "OWN")

    with _client_as(db_session, user_a) as c:
        resp = c.post(
            f"/v2/partials/quotes/{quote_a.id}/edit",
            data={"notes": "my own note"},
        )
    assert resp.status_code == 200, resp.text
    db_session.refresh(quote_a)
    assert quote_a.notes == "my own note"


def test_admin_sees_all_quotes(db_session, admin_user, test_customer_site):
    """ADMIN sees quotes regardless of requisition owner (no behaviour change)."""
    other_sales = _make_sales_user(db_session, "idor-other@trioscs.com", "azure-idor-other")
    _req, quote = _make_req_with_quote(db_session, other_sales, test_customer_site, "ADMIN")

    with _client_as(db_session, admin_user) as c:
        resp = c.post(
            f"/v2/partials/quotes/{quote.id}/edit",
            data={"notes": "admin edited"},
        )
    assert resp.status_code == 200, resp.text
    db_session.refresh(quote)
    assert quote.notes == "admin edited"


def test_buyer_sees_all_quotes(db_session, test_user, test_customer_site):
    """BUYER (test_user) sees quotes from another user's requisition (no scoping)."""
    other_sales = _make_sales_user(db_session, "idor-buyer-other@trioscs.com", "azure-idor-bother")
    _req, quote = _make_req_with_quote(db_session, other_sales, test_customer_site, "BUYER")

    with _client_as(db_session, test_user) as c:
        resp = c.post(f"/v2/partials/quotes/{quote.id}/preview")
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# QuoteLine-level IDOR tests (update, delete, apply-markup)
# ---------------------------------------------------------------------------


def _add_quote_line(db_session, quote: Quote, mpn: str = "TEST-MPN") -> QuoteLine:
    """Insert a QuoteLine for *quote* and return it."""
    line = QuoteLine(
        quote_id=quote.id,
        mpn=mpn,
        manufacturer="TestMfr",
        qty=10,
        cost_price=1.00,
        sell_price=1.25,
        margin_pct=20.0,
    )
    db_session.add(line)
    db_session.commit()
    db_session.refresh(line)
    return line


def test_sales_cannot_update_other_users_quote_line(db_session, test_customer_site):
    """SALES user A gets 404 trying to PUT a line belonging to user B's quote."""
    user_a = _make_sales_user(db_session, "line-upd-a@trioscs.com", "azure-line-upd-a")
    user_b = _make_sales_user(db_session, "line-upd-b@trioscs.com", "azure-line-upd-b")

    _req_b, quote_b = _make_req_with_quote(db_session, user_b, test_customer_site, "LINE-UPD")
    line_b = _add_quote_line(db_session, quote_b, mpn="LM741CN")

    with _client_as(db_session, user_a) as c:
        resp = c.put(
            f"/v2/partials/quotes/{quote_b.id}/lines/{line_b.id}",
            data={"mpn": "EVIL-MPN"},
        )
    assert resp.status_code == 404, resp.text

    # Verify the line was NOT mutated.
    db_session.refresh(line_b)
    assert line_b.mpn == "LM741CN"


def test_sales_can_update_own_quote_line(db_session, test_customer_site):
    """SALES user A can PUT a line on their OWN quote."""
    user_a = _make_sales_user(db_session, "line-own-upd@trioscs.com", "azure-line-own-upd")

    _req_a, quote_a = _make_req_with_quote(db_session, user_a, test_customer_site, "LINE-OWN-UPD")
    line_a = _add_quote_line(db_session, quote_a, mpn="LT1028CN")

    with _client_as(db_session, user_a) as c:
        resp = c.put(
            f"/v2/partials/quotes/{quote_a.id}/lines/{line_a.id}",
            data={"mpn": "LT1028CN-UPDATED"},
        )
    assert resp.status_code == 200, resp.text

    db_session.refresh(line_a)
    assert line_a.mpn == "LT1028CN-UPDATED"


def test_sales_cannot_delete_other_users_quote_line(db_session, test_customer_site):
    """SALES user A gets 404 trying to DELETE a line belonging to user B's quote."""
    user_a = _make_sales_user(db_session, "line-del-a@trioscs.com", "azure-line-del-a")
    user_b = _make_sales_user(db_session, "line-del-b@trioscs.com", "azure-line-del-b")

    _req_b, quote_b = _make_req_with_quote(db_session, user_b, test_customer_site, "LINE-DEL")
    line_b = _add_quote_line(db_session, quote_b, mpn="NE555P")

    with _client_as(db_session, user_a) as c:
        resp = c.delete(f"/v2/partials/quotes/{quote_b.id}/lines/{line_b.id}")
    assert resp.status_code == 404, resp.text

    # Line must still exist.
    surviving = db_session.get(QuoteLine, line_b.id)
    assert surviving is not None


def test_sales_can_delete_own_quote_line(db_session, test_customer_site):
    """SALES user A can DELETE a line on their OWN quote."""
    user_a = _make_sales_user(db_session, "line-own-del@trioscs.com", "azure-line-own-del")

    _req_a, quote_a = _make_req_with_quote(db_session, user_a, test_customer_site, "LINE-OWN-DEL")
    line_a = _add_quote_line(db_session, quote_a, mpn="TL072CP")

    with _client_as(db_session, user_a) as c:
        resp = c.delete(f"/v2/partials/quotes/{quote_a.id}/lines/{line_a.id}")
    assert resp.status_code == 200, resp.text

    assert db_session.get(QuoteLine, line_a.id) is None


def test_sales_cannot_apply_markup_to_other_users_quote(db_session, test_customer_site):
    """SALES user A gets 404 trying to apply markup to user B's quote."""
    user_a = _make_sales_user(db_session, "markup-a@trioscs.com", "azure-markup-a")
    user_b = _make_sales_user(db_session, "markup-b@trioscs.com", "azure-markup-b")

    _req_b, quote_b = _make_req_with_quote(db_session, user_b, test_customer_site, "MARKUP")
    line_b = _add_quote_line(db_session, quote_b, mpn="UA741CP")

    with _client_as(db_session, user_a) as c:
        resp = c.post(
            f"/v2/partials/quotes/{quote_b.id}/apply-markup",
            data={"markup_pct": "50"},
        )
    assert resp.status_code == 404, resp.text

    # Line sell_price must not have changed.
    db_session.refresh(line_b)
    assert float(line_b.sell_price) == 1.25


def test_admin_can_update_any_quote_line(db_session, admin_user, test_customer_site):
    """ADMIN can PUT a line on any quote regardless of requisition owner."""
    other_sales = _make_sales_user(db_session, "line-adm-other@trioscs.com", "azure-line-adm-other")
    _req, quote = _make_req_with_quote(db_session, other_sales, test_customer_site, "LINE-ADMIN")
    line = _add_quote_line(db_session, quote, mpn="CD4011BE")

    with _client_as(db_session, admin_user) as c:
        resp = c.put(
            f"/v2/partials/quotes/{quote.id}/lines/{line.id}",
            data={"mpn": "CD4011BE-ADMIN"},
        )
    assert resp.status_code == 200, resp.text

    db_session.refresh(line)
    assert line.mpn == "CD4011BE-ADMIN"


def test_admin_can_delete_any_quote_line(db_session, admin_user, test_customer_site):
    """ADMIN can DELETE a line on any quote regardless of requisition owner."""
    other_sales = _make_sales_user(db_session, "line-adm-del@trioscs.com", "azure-line-adm-del")
    _req, quote = _make_req_with_quote(db_session, other_sales, test_customer_site, "LINE-ADMIN-DEL")
    line = _add_quote_line(db_session, quote, mpn="MC14001BCP")

    with _client_as(db_session, admin_user) as c:
        resp = c.delete(f"/v2/partials/quotes/{quote.id}/lines/{line.id}")
    assert resp.status_code == 200, resp.text

    assert db_session.get(QuoteLine, line.id) is None

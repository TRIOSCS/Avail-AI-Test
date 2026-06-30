"""XSS regression: hand-built HTMLResponse fragments must HTML-escape
user-controlled free text (company / manufacturer names) so an injected payload
like </option><img onerror=...> can't execute when the fragment is HTMX-swapped.

These endpoints build HTML via f-strings (bypassing Jinja autoescape), so the
interpolated value must be html.escape()'d at the sink.

Called by: pytest
Depends on: app.routers.htmx.companies, app.routers.htmx.materials, conftest
"""

import html

from app.models.crm import Company

_XSS = "<img src=x onerror=alert(1)>"


def test_company_typeahead_escapes_company_name(client, db_session):
    db_session.add(Company(name=f"Acme {_XSS}", is_active=True))
    db_session.commit()
    resp = client.get("/v2/partials/customers/typeahead", params={"q": "Acme"})
    assert resp.status_code == 200
    assert _XSS not in resp.text  # raw payload must NOT be rendered
    assert html.escape(_XSS) in resp.text  # escaped form present


def test_check_company_duplicate_escapes_name(client, db_session):
    db_session.add(Company(name=f"Dup {_XSS}", is_active=True))
    db_session.commit()
    resp = client.get("/v2/partials/customers/check-duplicate", params={"name": f"Dup {_XSS}"})
    assert resp.status_code == 200
    assert _XSS not in resp.text
    assert "&lt;img" in resp.text


def test_manufacturer_add_escapes_reflected_name(client, db_session):
    resp = client.post("/v2/partials/manufacturers/add", data={"name": f"MFR{_XSS}"})
    assert resp.status_code == 200
    assert _XSS not in resp.text
    assert "&lt;img" in resp.text


def test_vendor_offers_tab_escapes_mpn_and_does_not_500(client, db_session, test_requisition, test_user):
    """vendor_tab binds a local `html` string var, so the escape must use the module
    alias — else any vendor-with-offers 500s (UnboundLocalError).

    Also o.mpn escaped.
    """
    from app.models import Offer, VendorCard

    vc = VendorCard(normalized_name="xss vend", display_name="XSS Vend")
    db_session.add(vc)
    db_session.commit()
    db_session.add(
        Offer(
            requisition_id=test_requisition.id,
            vendor_name="XSS Vend",
            mpn=_XSS,
            qty_available=10,
            unit_price=1.0,
            entered_by_id=test_user.id,
            status="pending_review",
            evidence_tier="T4",
        )
    )
    db_session.commit()
    resp = client.get(f"/v2/partials/vendors/{vc.id}/tab/offers")
    assert resp.status_code == 200  # regression: was UnboundLocalError 500
    assert _XSS not in resp.text
    assert "&lt;img" in resp.text


def test_quote_recent_terms_escapes_payment_terms(client, db_session, test_requisition):
    """Recent-terms serves DISTINCT terms across ALL quotes into every user's quote
    builder datalist — stored cross-user sink; payment_terms must be escaped."""
    from app.models.quotes import Quote

    db_session.add(
        Quote(
            requisition_id=test_requisition.id,
            quote_number="Q-XSS-1",
            line_items=[],
            status="draft",
            payment_terms=f"Net30 {_XSS}",
        )
    )
    db_session.commit()
    resp = client.get("/v2/partials/quotes/recent-terms")
    assert resp.status_code == 200
    assert _XSS not in resp.text
    assert "&lt;img" in resp.text

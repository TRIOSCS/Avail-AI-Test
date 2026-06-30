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

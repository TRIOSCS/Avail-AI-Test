# tests/test_quotes_relocation.py
"""Tests for the quotes relocation: standalone Quotes tab retired; quotes
surfaced on the requirement (Reqs workspace) and the CRM account.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone


def _req_with_part(db_session, test_user, *, company_id=None, customer_name="Acme Corp"):
    """Create a requisition (optionally linked to a company) with one part."""
    from app.models import Requirement, Requisition

    reqn = Requisition(
        name="Test Req",
        status="active",
        urgency="normal",
        customer_name=customer_name,
        company_id=company_id,
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(reqn)
    db_session.commit()
    db_session.refresh(reqn)

    part = Requirement(
        requisition_id=reqn.id,
        primary_mpn="MPN-001",
        target_qty=100,
        sourcing_status="open",
    )
    db_session.add(part)
    db_session.commit()
    db_session.refresh(part)
    return reqn, part


def _quote(db_session, *, requisition_id, number, site_id=None, status="draft"):
    """Create a Quote (minimal valid row)."""
    from app.models.quotes import Quote

    q = Quote(
        requisition_id=requisition_id,
        quote_number=number,
        customer_site_id=site_id,
        line_items=[],
        status=status,
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


def test_v2_quotes_bare_redirects_to_requisitions(client):
    resp = client.get("/v2/quotes", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/v2/requisitions"


def test_v2_quote_detail_still_renders(client, db_session, test_user):
    reqn, _ = _req_with_part(db_session, test_user)
    q = _quote(db_session, requisition_id=reqn.id, number="Q-DET-1")
    resp = client.get(f"/v2/quotes/{q.id}")
    assert resp.status_code == 200


def test_quotes_list_partial_removed(client):
    assert client.get("/v2/partials/quotes").status_code == 404


def test_reqs_page_has_no_quotes_nav_link(client):
    resp = client.get("/v2/requisitions")
    assert resp.status_code == 200
    assert 'href="/v2/quotes"' not in resp.text


def test_delete_quote_htmx_returns_hx_redirect(client, db_session, test_user):
    """DELETE handler must return 200 with HX-Redirect, not a 307 redirect."""
    reqn, _ = _req_with_part(db_session, test_user)
    q = _quote(db_session, requisition_id=reqn.id, number="Q-DEL-1", status="draft")
    resp = client.delete(f"/v2/partials/quotes/{q.id}")
    assert resp.status_code == 200
    assert resp.headers["HX-Redirect"] == "/v2/requisitions"


def test_part_quotes_tab_lists_requisition_quotes(client, db_session, test_user):
    reqn, part = _req_with_part(db_session, test_user)
    _quote(db_session, requisition_id=reqn.id, number="Q-WS-1")
    _quote(db_session, requisition_id=reqn.id, number="Q-WS-2")
    resp = client.get(f"/v2/partials/parts/{part.id}/tab/quotes")
    assert resp.status_code == 200
    assert "Q-WS-1" in resp.text
    assert "Q-WS-2" in resp.text


def test_part_quotes_tab_404_for_missing_requirement(client):
    assert client.get("/v2/partials/parts/999999/tab/quotes").status_code == 404


def test_part_quotes_tab_empty_state(client, db_session, test_user):
    _, part = _req_with_part(db_session, test_user)
    resp = client.get(f"/v2/partials/parts/{part.id}/tab/quotes")
    assert resp.status_code == 200
    assert "No quotes" in resp.text


def _company_with_site(db_session, *, name="Acme Corp"):
    from app.models.crm import Company, CustomerSite

    company = Company(name=name, is_active=True)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return company, site


def test_company_quotes_tab_unions_site_and_requisition(client, db_session, test_user):
    company, site = _company_with_site(db_session)
    reqn, _ = _req_with_part(db_session, test_user, company_id=company.id)
    # Quote linked only via the customer site:
    _quote(db_session, requisition_id=reqn.id, number="Q-SITE-1", site_id=site.id)
    # Quote linked only via the requisition (site is NULL) — must still appear:
    _quote(db_session, requisition_id=reqn.id, number="Q-REQONLY-1", site_id=None)
    resp = client.get(f"/v2/partials/customers/{company.id}/tab/quotes")
    assert resp.status_code == 200
    assert "Q-SITE-1" in resp.text
    assert "Q-REQONLY-1" in resp.text  # regression guard for the union fix


def test_company_quotes_tab_empty_state(client, db_session, test_user):
    company, _ = _company_with_site(db_session)
    resp = client.get(f"/v2/partials/customers/{company.id}/tab/quotes")
    assert resp.status_code == 200
    assert "No quotes" in resp.text


def test_company_unknown_tab_still_404(client, db_session, test_user):
    company, _ = _company_with_site(db_session)
    assert client.get(f"/v2/partials/customers/{company.id}/tab/bogus").status_code == 404


def test_company_detail_shows_quotes_tab_button(client, db_session, test_user):
    company, _ = _company_with_site(db_session)
    resp = client.get(f"/v2/partials/customers/{company.id}")
    assert resp.status_code == 200
    assert "tab/quotes" in resp.text

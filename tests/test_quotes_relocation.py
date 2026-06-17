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

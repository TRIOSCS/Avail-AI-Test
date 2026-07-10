# tests/test_quotes_relocation.py
"""Tests for the quotes relocation: standalone Quotes tab retired; quotes
surfaced on the requirement (Reqs workspace) and the CRM account.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import UTC, datetime


def _req_with_part(db_session, test_user, *, company_id=None, customer_name="Acme Corp"):
    """Create a requisition (optionally linked to a company) with one part."""
    from app.models import Requirement, Requisition

    reqn = Requisition(
        name="Test Req",
        status="open",
        urgency="normal",
        customer_name=customer_name,
        company_id=company_id,
        created_by=test_user.id,
        created_at=datetime.now(UTC),
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


def _quote_line(db_session, *, quote_id, mpn, material_card_id=None, qty=10):
    from app.models.quotes import QuoteLine

    ql = QuoteLine(
        quote_id=quote_id,
        mpn=mpn,
        material_card_id=material_card_id,
        qty=qty,
        sell_price=5,
        margin_pct=20,
    )
    db_session.add(ql)
    db_session.commit()
    db_session.refresh(ql)
    return ql


def test_delete_quote_with_lines_cascades_no_integrity_error(db_session, test_user):
    """Deleting a quote that HAS QuoteLines must cascade-delete the lines, not raise
    IntegrityError.

    quote_lines.quote_id is NOT NULL with ondelete=CASCADE, so the ORM relationship
    needs cascade='all, delete-orphan' + passive_deletes=True; otherwise the unit-of-
    work NULLs the children first and violates NOT NULL. (The existing delete test only
    covered line-less quotes, which is why this hid.)
    """
    from app.models.quotes import Quote, QuoteLine

    reqn, _ = _req_with_part(db_session, test_user)
    q = _quote(db_session, requisition_id=reqn.id, number="Q-CASC-1")
    line = _quote_line(db_session, quote_id=q.id, mpn="LM317T")
    line_id, quote_id = line.id, q.id

    db_session.delete(q)
    db_session.commit()

    assert db_session.get(Quote, quote_id) is None
    assert db_session.get(QuoteLine, line_id) is None, "lines must cascade-delete with the quote"


def _make_material_card(db_session):
    """Create a minimal valid MaterialCard (normalized_mpn and display_mpn are NOT
    NULL)."""
    from app.models.intelligence import MaterialCard

    # Unique per call so the helper is safe even if a test creates several cards.
    mpn = f"CARD-MPN-{db_session.query(MaterialCard).count() + 1}"
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn,
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


def test_part_quotes_tab_lists_quotes_with_this_mpn_across_requisitions(client, db_session, test_user):
    r1, part1 = _req_with_part(db_session, test_user, customer_name="CompA")  # part1.primary_mpn == "MPN-001"
    r2, _ = _req_with_part(db_session, test_user, customer_name="CompB")
    q = _quote(db_session, requisition_id=r2.id, number="Q-OTHERREQ")  # quote lives on a DIFFERENT requisition
    _quote_line(db_session, quote_id=q.id, mpn="MPN-001")
    resp = client.get(f"/v2/partials/parts/{part1.id}/tab/quotes")
    assert resp.status_code == 200
    assert "Q-OTHERREQ" in resp.text  # cross-requisition match by MPN
    assert "CompB" in resp.text  # customer of the matching quote shown


def test_part_quotes_tab_matches_substitute_mpn(client, db_session, test_user):
    r1, part1 = _req_with_part(db_session, test_user)
    part1.substitutes = [{"mpn": "SUB-999", "manufacturer": "X"}]
    db_session.commit()
    r2, _ = _req_with_part(db_session, test_user)
    q = _quote(db_session, requisition_id=r2.id, number="Q-SUBMATCH")
    _quote_line(db_session, quote_id=q.id, mpn="SUB-999")
    resp = client.get(f"/v2/partials/parts/{part1.id}/tab/quotes")
    assert resp.status_code == 200
    assert "Q-SUBMATCH" in resp.text


def test_part_quotes_tab_matches_canonical_material_card(client, db_session, test_user):
    # Build a minimal MaterialCard, attach it to the part and to a quote line with a DIFFERENT mpn.
    card = _make_material_card(db_session)
    r1, part1 = _req_with_part(db_session, test_user)
    part1.material_card_id = card.id
    db_session.commit()
    r2, _ = _req_with_part(db_session, test_user)
    q = _quote(db_session, requisition_id=r2.id, number="Q-CARDMATCH")
    _quote_line(db_session, quote_id=q.id, mpn="DIFFERENT-MPN", material_card_id=card.id)
    resp = client.get(f"/v2/partials/parts/{part1.id}/tab/quotes")
    assert resp.status_code == 200
    assert "Q-CARDMATCH" in resp.text  # matched via canonical material_card, not MPN


def test_part_quotes_tab_excludes_unrelated_mpn(client, db_session, test_user):
    r1, part1 = _req_with_part(db_session, test_user)  # primary_mpn == "MPN-001"
    r2, _ = _req_with_part(db_session, test_user)
    q = _quote(db_session, requisition_id=r2.id, number="Q-UNRELATED")
    _quote_line(db_session, quote_id=q.id, mpn="ZZZ-000")
    resp = client.get(f"/v2/partials/parts/{part1.id}/tab/quotes")
    assert resp.status_code == 200
    assert "Q-UNRELATED" not in resp.text


def test_part_quotes_tab_empty_state(client, db_session, test_user):
    _, part = _req_with_part(db_session, test_user)
    resp = client.get(f"/v2/partials/parts/{part.id}/tab/quotes")
    assert resp.status_code == 200
    assert "hasn't been on any quotes" in resp.text


def test_part_quotes_tab_404_for_missing_requirement(client):
    assert client.get("/v2/partials/parts/999999/tab/quotes").status_code == 404


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
    company.account_owner_id = test_user.id  # company detail/tab now gates on can_manage_account
    db_session.commit()
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
    company.account_owner_id = test_user.id  # company detail/tab now gates on can_manage_account
    db_session.commit()
    resp = client.get(f"/v2/partials/customers/{company.id}/tab/quotes")
    assert resp.status_code == 200
    assert "No quotes" in resp.text


def test_company_unknown_tab_still_404(client, db_session, test_user):
    company, _ = _company_with_site(db_session)
    assert client.get(f"/v2/partials/customers/{company.id}/tab/bogus").status_code == 404


def test_company_detail_shows_quotes_tab_button(client, db_session, test_user):
    company, _ = _company_with_site(db_session)
    company.account_owner_id = test_user.id  # company detail/tab now gates on can_manage_account
    db_session.commit()
    resp = client.get(f"/v2/partials/customers/{company.id}")
    assert resp.status_code == 200
    assert "tab/quotes" in resp.text

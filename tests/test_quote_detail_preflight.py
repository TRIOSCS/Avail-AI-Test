"""test_quote_detail_preflight.py — B3: preflight advisories on the quote detail page.

Covers:
- draft quote with a DNC-flagged customer site -> detail GET renders the amber
  advisory card with the DNC message.
- non-draft (status='sent') quote, even with a DNC-flagged site -> no card
  (preflight_warnings is only populated for drafts; see quote_detail_partial).
- draft quote with no warnings -> no card.

Called by: pytest
Depends on: conftest.py fixtures, app.routers.htmx.quotes.quote_detail_partial,
            app.services.quote_preflight.quote_preflight
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import CustomerSite, Quote, Requisition, User

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def draft_quote_dnc_site(
    db_session: Session,
    test_requisition: Requisition,
    test_customer_site: CustomerSite,
    test_user: User,
) -> Quote:
    """A draft quote whose customer site is marked Do-Not-Contact (cheapest preflight
    trigger — quote_preflight's first check)."""
    test_customer_site.do_not_contact = True
    db_session.add(test_customer_site)
    q = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        quote_number="TEST-Q-PREFLIGHT-DRAFT-DNC",
        status="draft",
        line_items=[],
        created_by_id=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


@pytest.fixture()
def sent_quote_dnc_site(
    db_session: Session,
    test_requisition: Requisition,
    test_customer_site: CustomerSite,
    test_user: User,
) -> Quote:
    """A SENT quote whose customer site is DNC-flagged — the trigger fires but the quote
    is not a draft, so preflight_warnings must stay empty."""
    test_customer_site.do_not_contact = True
    db_session.add(test_customer_site)
    q = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        quote_number="TEST-Q-PREFLIGHT-SENT-DNC",
        status="sent",
        line_items=[],
        created_by_id=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


@pytest.fixture()
def draft_quote_clean(
    db_session: Session,
    test_requisition: Requisition,
    test_customer_site: CustomerSite,
    test_user: User,
) -> Quote:
    """A draft quote with no preflight triggers at all (site not DNC, no lines)."""
    q = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        quote_number="TEST-Q-PREFLIGHT-DRAFT-CLEAN",
        status="draft",
        line_items=[],
        created_by_id=test_user.id,
        created_at=datetime.now(UTC),
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


# ── Tests ────────────────────────────────────────────────────────────


class TestQuoteDetailPreflightAdvisories:
    def test_draft_with_dnc_site_shows_amber_card(self, client: TestClient, draft_quote_dnc_site: Quote):
        resp = client.get(f"/v2/partials/quotes/{draft_quote_dnc_site.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Before you send" in resp.text
        assert "Do-Not-Contact" in resp.text

    def test_sent_quote_never_shows_card_even_with_trigger(self, client: TestClient, sent_quote_dnc_site: Quote):
        resp = client.get(f"/v2/partials/quotes/{sent_quote_dnc_site.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Before you send" not in resp.text

    def test_draft_with_no_warnings_shows_no_card(self, client: TestClient, draft_quote_clean: Quote):
        resp = client.get(f"/v2/partials/quotes/{draft_quote_clean.id}", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "Before you send" not in resp.text

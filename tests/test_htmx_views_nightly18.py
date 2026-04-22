"""tests/test_htmx_views_nightly18.py — Coverage for customers list/detail/tabs and
quotes.

Targets:
  - companies_list_partial
  - company_create_form / create_company
  - company_typeahead / check_company_duplicate
  - company_detail_partial
  - company_tab (sites/contacts/requisitions/activity)
  - preview_quote / delete_quote_htmx / reopen_quote / recent_terms

Called by: pytest autodiscovery
Depends on: conftest.py fixtures, app.routers.htmx_views
"""

import os

os.environ["TESTING"] = "1"

import uuid

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import QuoteStatus
from app.models import Company, Requisition, User
from app.models.quotes import Quote

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_company(db: Session, **kw) -> Company:
    defaults = dict(
        name=f"TestCo-{uuid.uuid4().hex[:8]}",
        is_active=True,
    )
    defaults.update(kw)
    c = Company(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_quote(db: Session, req: Requisition, user: User, status: str = QuoteStatus.DRAFT, **kw) -> Quote:
    defaults = dict(
        requisition_id=req.id,
        quote_number=f"Q-{uuid.uuid4().hex[:8]}",
        status=status,
        created_by_id=user.id,
    )
    defaults.update(kw)
    q = Quote(**defaults)
    db.add(q)
    db.commit()
    db.refresh(q)
    return q


# ── Companies List ────────────────────────────────────────────────────────


class TestCompaniesListPartial:
    def test_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200

    def test_list_with_company(self, client: TestClient, db_session: Session):
        _make_company(db_session)
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200

    def test_list_with_search(self, client: TestClient, db_session: Session):
        company = _make_company(db_session, name="UniqueSearchableCo")
        resp = client.get("/v2/partials/customers?search=UniqueSearch")
        assert resp.status_code == 200

    def test_list_pagination(self, client: TestClient):
        resp = client.get("/v2/partials/customers?limit=10&offset=5")
        assert resp.status_code == 200


# ── Company Create Form ───────────────────────────────────────────────────


class TestCompanyCreateForm:
    def test_get_create_form(self, client: TestClient):
        resp = client.get("/v2/partials/customers/create-form")
        assert resp.status_code == 200


# ── Create Company ────────────────────────────────────────────────────────


class TestCreateCompany:
    def test_create_success(self, client: TestClient, db_session: Session):
        unique_name = f"NewCo-{uuid.uuid4().hex[:8]}"
        resp = client.post(
            "/v2/partials/customers/create",
            data={"name": unique_name, "website": "https://example.com"},
        )
        assert resp.status_code == 200

    def test_create_missing_name(self, client: TestClient):
        resp = client.post("/v2/partials/customers/create", data={"name": ""})
        assert resp.status_code == 400

    def test_create_duplicate(self, client: TestClient, db_session: Session):
        company = _make_company(db_session)
        resp = client.post("/v2/partials/customers/create", data={"name": company.name})
        assert resp.status_code == 409


# ── Company Typeahead ─────────────────────────────────────────────────────


class TestCompanyTypeahead:
    def test_typeahead_empty(self, client: TestClient):
        resp = client.get("/v2/partials/customers/typeahead?q=")
        assert resp.status_code == 200
        assert resp.content == b""

    def test_typeahead_short_query(self, client: TestClient):
        resp = client.get("/v2/partials/customers/typeahead?q=a")
        assert resp.status_code == 200
        assert resp.content == b""

    def test_typeahead_with_results(self, client: TestClient, db_session: Session):
        company = _make_company(db_session, name="TypeaheadMatchCo")
        resp = client.get("/v2/partials/customers/typeahead?q=Typeahead")
        assert resp.status_code == 200


# ── Check Duplicate ───────────────────────────────────────────────────────


class TestCheckCompanyDuplicate:
    def test_check_empty_name(self, client: TestClient):
        resp = client.get("/v2/partials/customers/check-duplicate?name=")
        assert resp.status_code == 200
        assert resp.content == b""

    def test_check_no_duplicate(self, client: TestClient):
        resp = client.get("/v2/partials/customers/check-duplicate?name=NonexistentXYZ999")
        assert resp.status_code == 200
        assert resp.content == b""

    def test_check_found_duplicate(self, client: TestClient, db_session: Session):
        company = _make_company(db_session, name="DupCheckCo")
        resp = client.get(f"/v2/partials/customers/check-duplicate?name={company.name}")
        assert resp.status_code == 200
        assert b"already exists" in resp.content


# ── Company Detail ────────────────────────────────────────────────────────


class TestCompanyDetailPartial:
    def test_detail_success(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}")
        assert resp.status_code == 200

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/customers/99999")
        assert resp.status_code == 404


# ── Company Tabs ──────────────────────────────────────────────────────────


class TestCompanyTab:
    def test_tab_sites(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/sites")
        assert resp.status_code == 200

    def test_tab_contacts(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/contacts")
        assert resp.status_code == 200

    def test_tab_requisitions(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/requisitions")
        assert resp.status_code == 200

    def test_tab_activity(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/activity")
        assert resp.status_code == 200

    def test_tab_invalid(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tab/fakeview")
        assert resp.status_code == 404

    def test_tab_company_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/customers/99999/tab/sites")
        assert resp.status_code == 404


# ── Preview Quote ─────────────────────────────────────────────────────────


class TestPreviewQuote:
    def test_preview_success(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        quote = _make_quote(db_session, test_requisition, test_user)
        resp = client.post(f"/v2/partials/quotes/{quote.id}/preview")
        assert resp.status_code == 200

    def test_preview_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/quotes/99999/preview")
        assert resp.status_code == 404


# ── Delete Quote ──────────────────────────────────────────────────────────


class TestDeleteQuote:
    def test_delete_draft_quote(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        quote = _make_quote(db_session, test_requisition, test_user, status=QuoteStatus.DRAFT)
        qid = quote.id
        resp = client.delete(f"/v2/partials/quotes/{qid}")
        assert resp.status_code == 200
        assert db_session.get(Quote, qid) is None

    def test_delete_non_draft(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        quote = _make_quote(db_session, test_requisition, test_user, status=QuoteStatus.SENT)
        resp = client.delete(f"/v2/partials/quotes/{quote.id}")
        assert resp.status_code == 400

    def test_delete_not_found(self, client: TestClient):
        resp = client.delete("/v2/partials/quotes/99999")
        assert resp.status_code == 404


# ── Reopen Quote ──────────────────────────────────────────────────────────


class TestReopenQuote:
    def test_reopen_sent_quote(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        quote = _make_quote(db_session, test_requisition, test_user, status=QuoteStatus.SENT)
        resp = client.post(f"/v2/partials/quotes/{quote.id}/reopen")
        assert resp.status_code == 200
        db_session.refresh(quote)
        assert quote.status == QuoteStatus.DRAFT

    def test_reopen_draft_quote_rejected(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_user: User
    ):
        quote = _make_quote(db_session, test_requisition, test_user, status=QuoteStatus.DRAFT)
        resp = client.post(f"/v2/partials/quotes/{quote.id}/reopen")
        assert resp.status_code == 400

    def test_reopen_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/quotes/99999/reopen")
        assert resp.status_code == 404


# ── Recent Terms ──────────────────────────────────────────────────────────


class TestRecentTerms:
    def test_recent_terms_empty(self, client: TestClient):
        resp = client.get("/v2/partials/quotes/recent-terms")
        assert resp.status_code == 200

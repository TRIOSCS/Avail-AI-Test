"""
test_htmx_requisitions_list.py — Tests for Phase 3 Task 3: Requisitions list + create modal.
Verifies requisitions list page, rows partial with search/filter/sort/pagination,
and create modal form rendering.
Called by: pytest
Depends on: app/routers/views.py, app/templates/partials/requisitions/
"""

import os

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("USE_HTMX", "true")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import Requirement, Requisition


@pytest.fixture()
def htmx_client(db_session, test_user):
    """TestClient with views router registered and auth overridden."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app
    from app.routers.views import router as views_router

    route_paths = [r.path for r in app.routes]
    if "/views/requisitions" not in route_paths:
        app.include_router(views_router)

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def sample_requisitions(db_session, test_user):
    """Create multiple requisitions for list/filter/sort/pagination tests."""
    reqs = []
    for i, (name, customer, status) in enumerate([
        ("Alpha Request", "Acme Corp", "open"),
        ("Beta Order", "Globex Inc", "draft"),
        ("Gamma RFQ", "Acme Corp", "won"),
        ("Delta Inquiry", "Initech", "open"),
        ("Epsilon Bid", "Globex Inc", "archived"),
    ]):
        r = Requisition(
            name=name,
            customer_name=customer,
            status=status,
            created_by=test_user.id,
            created_at=datetime(2026, 3, 1 + i, tzinfo=timezone.utc),
        )
        db_session.add(r)
        db_session.flush()
        # Add one requirement to each
        db_session.add(Requirement(
            requisition_id=r.id,
            primary_mpn=f"PART-{i:03d}",
            target_qty=100,
            created_at=datetime.now(timezone.utc),
        ))
        reqs.append(r)
    db_session.commit()
    for r in reqs:
        db_session.refresh(r)
    return reqs


class TestRequisitionsListPage:
    """Tests for GET /views/requisitions — full page HTML."""

    def test_requisitions_list_page_returns_html(self, htmx_client):
        resp = htmx_client.get("/views/requisitions")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_requisitions_list_page_contains_table(self, htmx_client):
        resp = htmx_client.get("/views/requisitions")
        assert "req-table-body" in resp.text
        assert "Requisitions" in resp.text

    def test_requisitions_list_page_has_new_button(self, htmx_client):
        resp = htmx_client.get("/views/requisitions")
        assert "New Requisition" in resp.text
        assert "create-form" in resp.text

    def test_requisitions_list_shows_data(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions")
        assert resp.status_code == 200
        # Default filter excludes archived/won/lost — should see Alpha, Beta, Delta
        assert "Alpha Request" in resp.text
        assert "Beta Order" in resp.text
        assert "Delta Inquiry" in resp.text

    def test_requisitions_list_has_search_input(self, htmx_client):
        resp = htmx_client.get("/views/requisitions")
        assert 'type="search"' in resp.text
        assert "delay:300ms" in resp.text

    def test_requisitions_list_has_quick_filters(self, htmx_client):
        resp = htmx_client.get("/views/requisitions")
        for label in ["All", "Open", "Draft", "Awarded", "Archived"]:
            assert label in resp.text

    def test_requisitions_list_has_sortable_headers(self, htmx_client):
        resp = htmx_client.get("/views/requisitions")
        assert "sortable" in resp.text
        assert "sort=name" in resp.text
        assert "sort=created_at" in resp.text


class TestRequisitionsRowsPartial:
    """Tests for GET /views/requisitions/rows — HTMX rows swap target."""

    def test_rows_returns_html(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_rows_contain_requisition_names(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows")
        # Default: non-archived. Alpha (open), Beta (draft), Delta (open) expected
        assert "Alpha Request" in resp.text
        assert "Beta Order" in resp.text
        assert "Delta Inquiry" in resp.text

    def test_rows_exclude_archived_by_default(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows")
        assert "Epsilon Bid" not in resp.text  # archived
        assert "Gamma RFQ" not in resp.text    # won

    def test_rows_search_filters_by_name(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows?q=Alpha")
        assert "Alpha Request" in resp.text
        assert "Beta Order" not in resp.text

    def test_rows_search_filters_by_customer(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows?q=Globex")
        assert "Beta Order" in resp.text
        assert "Alpha Request" not in resp.text

    def test_rows_status_filter_open(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows?status=open")
        assert "Alpha Request" in resp.text
        assert "Delta Inquiry" in resp.text
        assert "Beta Order" not in resp.text  # draft

    def test_rows_status_filter_archived(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows?status=archived")
        assert "Epsilon Bid" in resp.text
        assert "Gamma RFQ" in resp.text  # won is in archived group

    def test_rows_sort_by_name_asc(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows?sort=name&dir=asc")
        text = resp.text
        # Alpha should appear before Beta, Beta before Delta
        alpha_pos = text.index("Alpha Request")
        beta_pos = text.index("Beta Order")
        delta_pos = text.index("Delta Inquiry")
        assert alpha_pos < beta_pos < delta_pos

    def test_rows_sort_by_name_desc(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows?sort=name&dir=desc")
        text = resp.text
        alpha_pos = text.index("Alpha Request")
        delta_pos = text.index("Delta Inquiry")
        assert delta_pos < alpha_pos

    def test_rows_empty_shows_empty_state(self, htmx_client):
        resp = htmx_client.get("/views/requisitions/rows?q=nonexistent_xyz")
        assert resp.status_code == 200
        assert "No requisitions found" in resp.text


class TestRequisitionsPagination:
    """Tests for page param on requisitions rows."""

    def test_page_1_returns_results(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows?page=1")
        assert resp.status_code == 200
        # All non-archived fit on one page (3 items < PER_PAGE=25)
        assert "Alpha Request" in resp.text

    def test_page_beyond_range_clamps(self, htmx_client, sample_requisitions):
        resp = htmx_client.get("/views/requisitions/rows?page=999")
        assert resp.status_code == 200
        # Should clamp to last page, still return results
        assert "text/html" in resp.headers["content-type"]


class TestCreateFormPartial:
    """Tests for GET /views/requisitions/create-form — modal form."""

    def test_create_form_returns_html(self, htmx_client):
        resp = htmx_client.get("/views/requisitions/create-form")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_create_form_has_name_input(self, htmx_client):
        resp = htmx_client.get("/views/requisitions/create-form")
        assert 'name="name"' in resp.text
        assert "Requisition name" in resp.text

    def test_create_form_has_customer_input(self, htmx_client):
        resp = htmx_client.get("/views/requisitions/create-form")
        assert 'name="customer_name"' in resp.text

    def test_create_form_has_submit_button(self, htmx_client):
        resp = htmx_client.get("/views/requisitions/create-form")
        assert "Create" in resp.text
        assert "Cancel" in resp.text

    def test_create_form_posts_to_api(self, htmx_client):
        resp = htmx_client.get("/views/requisitions/create-form")
        assert 'hx-post="/api/requisitions"' in resp.text

    def test_create_form_has_alpine_bindings(self, htmx_client):
        resp = htmx_client.get("/views/requisitions/create-form")
        assert "x-data" in resp.text
        assert "x-model" in resp.text

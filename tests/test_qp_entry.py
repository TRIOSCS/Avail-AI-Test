"""test_qp_entry.py — TDD tests for the QP front door.

Covers GET /v2/qp/for-buy-plan/{bp_id} (get-or-create the buy plan's Quality Plan and
render the native QP detail) plus the "Quality Plan" entry button on the buy-plan detail.

Tests:
  1. First open with no existing QP creates one (DRAFT, buy_plan_id set) and renders the
     QP detail (200).
  2. Idempotent — a second open returns the SAME qp.id and does not create a duplicate.
  3. Ownership — a restricted-role (sales) user who does not own the buy plan's
     requisition gets a 404 and NO QP is created.
  4. The buy-plan detail renders the "Quality Plan" button wired to the for-buy-plan
     get-or-open route.

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_qp_entry.py -v)
Depends on: app.routers.quality_plans, app.routers.htmx_views (buy-plan detail partial),
            conftest (client, db_session, test_user, sales_user, test_customer_site).
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.quality_plan import QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition

# HTMX navigations send this header; the for-buy-plan route content-negotiates on it
# (bare partial for HTMX callers, full app shell for a raw browser reload/bookmark).
_HX = {"HX-Request": "true"}

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_requisition(db: Session, owner_id: int) -> Requisition:
    req = Requisition(
        name="QP-ENTRY-001",
        status="active",
        customer_name="Acme Electronics",
        created_by=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_quote(db: Session, requisition_id: int, site_id: int | None = None) -> Quote:
    q = Quote(
        requisition_id=requisition_id,
        customer_site_id=site_id,
        quote_number="QT-QPE-001",
        status="sent",
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    return q


def _make_buy_plan(db: Session, requisition_id: int, quote_id: int, owner_id: int) -> BuyPlan:
    bp = BuyPlan(
        requisition_id=requisition_id,
        quote_id=quote_id,
        status="draft",
        so_status="pending",
        sales_order_number="SO-QPE-1",
        submitted_by_id=owner_id,
    )
    db.add(bp)
    db.flush()
    return bp


def _seed_buy_plan(db: Session, owner_id: int, site_id: int) -> BuyPlan:
    """Create a requisition → quote → buy plan chain owned by owner_id."""
    req = _make_requisition(db, owner_id)
    q = _make_quote(db, req.id, site_id)
    bp = _make_buy_plan(db, req.id, q.id, owner_id)
    db.commit()
    return bp


def _qp_count(db: Session, bp_id: int) -> int:
    """Number of QualityPlan rows linked to the given buy plan."""
    return db.execute(
        select(func.count()).select_from(QualityPlan).where(QualityPlan.buy_plan_id == bp_id)
    ).scalar_one()


def _qps_for(db: Session, bp_id: int) -> list[QualityPlan]:
    """All QualityPlan rows linked to the given buy plan."""
    return list(db.execute(select(QualityPlan).where(QualityPlan.buy_plan_id == bp_id)).scalars().all())


def _restricted_client(db_session: Session, sales_user: User) -> TestClient:
    """A TestClient authenticated as a restricted-role (sales) user.

    Mirrors the conftest `client` fixture override pattern but returns sales_user from
    require_user so role-scoped ownership (RESTRICTED_ROLES) is exercised.
    """
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: sales_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)


@pytest.fixture()
def restricted_client(db_session: Session, sales_user: User):
    yield from _restricted_client(db_session, sales_user)


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_for_buy_plan_creates_qp_on_first_open(client, db_session: Session, test_user, test_customer_site):
    """First open with no existing QP creates one (DRAFT, buy_plan_id set) and
    renders."""
    bp = _seed_buy_plan(db_session, test_user.id, test_customer_site.id)
    assert _qp_count(db_session, bp.id) == 0

    resp = client.get(f"/v2/qp/for-buy-plan/{bp.id}", headers=_HX)
    assert resp.status_code == 200

    qps = _qps_for(db_session, bp.id)
    assert len(qps) == 1
    qp = qps[0]
    assert qp.status == "draft"
    assert qp.created_by_id == test_user.id
    # Lands on the native QP detail view.
    assert f"Quality Plan #{qp.id}" in resp.text


def test_for_buy_plan_is_idempotent(client, db_session: Session, test_user, test_customer_site):
    """A second open returns the SAME qp.id and creates no duplicate row."""
    bp = _seed_buy_plan(db_session, test_user.id, test_customer_site.id)

    resp1 = client.get(f"/v2/qp/for-buy-plan/{bp.id}", headers=_HX)
    assert resp1.status_code == 200
    qp_id_1 = _qps_for(db_session, bp.id)[0].id

    resp2 = client.get(f"/v2/qp/for-buy-plan/{bp.id}", headers=_HX)
    assert resp2.status_code == 200

    qps = _qps_for(db_session, bp.id)
    assert len(qps) == 1, "second open must not create a duplicate QP"
    assert qps[0].id == qp_id_1
    assert f"Quality Plan #{qp_id_1}" in resp2.text


def test_for_buy_plan_missing_buy_plan_404(client):
    """A non-existent buy plan returns 404 (no QP to create)."""
    resp = client.get("/v2/qp/for-buy-plan/999999", headers=_HX)
    assert resp.status_code == 404


def test_for_buy_plan_ownership_404_for_restricted_non_owner(
    restricted_client, db_session: Session, test_user, test_customer_site
):
    """A restricted-role user who does not own the buy plan's requisition gets 404.

    The buy plan is owned by test_user (a buyer); the acting user is sales_user
    (RESTRICTED_ROLES), who owns nothing here. The route must 404 and create NO QP.
    """
    bp = _seed_buy_plan(db_session, test_user.id, test_customer_site.id)

    resp = restricted_client.get(f"/v2/qp/for-buy-plan/{bp.id}", headers=_HX)
    assert resp.status_code == 404
    # Ownership is enforced before create — no QP row leaks into existence.
    assert _qp_count(db_session, bp.id) == 0


def test_buy_plan_detail_renders_quality_plan_button(client, db_session: Session, test_user, test_customer_site):
    """The buy-plan detail renders the 'Quality Plan' button wired to for-buy-plan."""
    bp = _seed_buy_plan(db_session, test_user.id, test_customer_site.id)

    resp = client.get(f"/v2/partials/buy-plans/{bp.id}")
    assert resp.status_code == 200
    body = resp.text
    assert "Quality Plan" in body
    assert f"/v2/qp/for-buy-plan/{bp.id}" in body


def test_for_buy_plan_full_page_reload_serves_shell(client, db_session: Session, test_user, test_customer_site):
    """SET-05: the 'Quality Plan' button hx-push-urls /v2/qp/for-buy-plan/{id}, so a raw
    browser reload / bookmark of that url (no HX-Request header) must render the full app
    shell that HTMX-loads the QP — not a shell-less fragment.

    The shell pass must also be side-effect-free: get-or-create runs only when the shell's
    loader re-requests WITH the HX-Request header.
    """
    bp = _seed_buy_plan(db_session, test_user.id, test_customer_site.id)
    assert _qp_count(db_session, bp.id) == 0

    resp = client.get(f"/v2/qp/for-buy-plan/{bp.id}")  # no HX-Request → full page
    assert resp.status_code == 200
    body = resp.text
    # App shell: the #main-content mount + a loader that points back at this same url.
    assert 'id="main-content"' in body
    assert f'hx-get="/v2/qp/for-buy-plan/{bp.id}"' in body
    assert 'hx-trigger="load"' in body
    # The QP partial has NOT been rendered inline (it loads via the HTMX pass)...
    assert "Quality Plan #" not in body
    # ...and no QP row was created by the side-effect-free full-page load.
    assert _qp_count(db_session, bp.id) == 0

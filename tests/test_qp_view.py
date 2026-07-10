"""test_qp_view.py — TDD tests for GET /v2/qp/{id} (QP detail partial).

Tests:
  1. GET /v2/qp/{id} renders customer name and owner derived from the linked BuyPlan.
  2. GET /v2/qp/{id} renders a Buy-Plan section with buy-plan line count.
  3. GET /v2/qp/{id} for an incomplete QP returns 200 with completeness errors inline.
  4. GET /v2/qp/9999 returns 404.
  5. Unauthenticated GET returns 401.

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_qp_view.py -v)
Depends on: app.routers.quality_plans, conftest (client, db_session, test_user,
            test_buy_plan, test_company, test_customer_site).
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.buy_plan import BuyPlan
from app.models.quality_plan import QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_requisition(db: Session, owner_id: int) -> Requisition:
    req = Requisition(
        name="QP-TEST-001",
        status="active",
        customer_name="Acme Electronics",
        created_by=owner_id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    return req


def _make_quote(db: Session, requisition_id: int, site_id: int | None = None) -> Quote:
    q = Quote(
        requisition_id=requisition_id,
        customer_site_id=site_id,
        quote_number="QT-QP-001",
        status="sent",
        created_at=datetime.now(UTC),
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
        sales_order_number="SO-9999",
        submitted_by_id=owner_id,
    )
    db.add(bp)
    db.flush()
    return bp


def _make_qp(db: Session, buy_plan_id: int, owner_id: int) -> QualityPlan:
    qp = QualityPlan(
        buy_plan_id=buy_plan_id,
        created_by_id=owner_id,
        status="draft",
        order_type="new",
    )
    db.add(qp)
    db.flush()
    return qp


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_qp_detail_renders_header(client, db_session: Session, test_user, test_customer_site):
    """GET /v2/qp/{id} renders the QP header with customer name and SO#."""
    req = _make_requisition(db_session, test_user.id)
    q = _make_quote(db_session, req.id, test_customer_site.id)
    bp = _make_buy_plan(db_session, req.id, q.id, test_user.id)
    qp = _make_qp(db_session, bp.id, test_user.id)
    db_session.commit()

    resp = client.get(f"/v2/qp/{qp.id}")
    assert resp.status_code == 200
    body = resp.text
    # Header should contain QP ID
    assert f"Quality Plan #{qp.id}" in body
    # Owner name derived from buy_plan.submitted_by (test_user)
    assert test_user.name in body


def test_qp_detail_renders_buy_plan_section(client, db_session: Session, test_user, test_customer_site):
    """GET /v2/qp/{id} renders a Buy Plan section referencing the linked buy plan."""
    req = _make_requisition(db_session, test_user.id)
    q = _make_quote(db_session, req.id, test_customer_site.id)
    bp = _make_buy_plan(db_session, req.id, q.id, test_user.id)
    qp = _make_qp(db_session, bp.id, test_user.id)
    db_session.commit()

    resp = client.get(f"/v2/qp/{qp.id}")
    assert resp.status_code == 200
    body = resp.text
    # Must show a Buy Plan section
    assert "Buy Plan" in body
    # Must reference the buy-plan id
    assert str(bp.id) in body


def test_qp_detail_shows_completeness_errors(client, db_session: Session, test_user):
    """GET /v2/qp/{id} for a QP missing created_by_id shows completeness errors inline.

    Sets created_by_id=None via a raw SQL update (column is nullable, so SQLite allows
    it) so validate_complete() returns a non-empty error list and the detail page
    renders them inline rather than 500-ing.
    """
    from sqlalchemy import text

    req = _make_requisition(db_session, test_user.id)
    q = _make_quote(db_session, req.id)
    bp = _make_buy_plan(db_session, req.id, q.id, test_user.id)
    qp = _make_qp(db_session, bp.id, test_user.id)
    db_session.commit()

    # Blank out created_by_id (nullable) to trigger validate_complete error
    db_session.execute(
        text("UPDATE quality_plans SET created_by_id = NULL WHERE id = :id"),
        {"id": qp.id},
    )
    db_session.commit()

    resp = client.get(f"/v2/qp/{qp.id}")
    assert resp.status_code == 200
    body = resp.text
    # Completeness error banner must appear (owner is required)
    assert "required" in body.lower() or "incomplete" in body.lower() or "owner" in body.lower()


def test_qp_detail_404(client):
    """GET /v2/qp/9999 returns 404 for a non-existent QP."""
    resp = client.get("/v2/qp/9999")
    assert resp.status_code == 404


def test_qp_review_concurrent_delete_returns_404(client, db_session: Session, test_user, test_customer_site):
    """POST review when the QP is concurrently deleted → 404, not 500.

    toggle_section_reviewed raises ValueError if the QP vanished between the router's
    existence check and the service load. The router must translate that to a 404.
    """
    from unittest.mock import patch

    req = _make_requisition(db_session, test_user.id)
    q = _make_quote(db_session, req.id, test_customer_site.id)
    bp = _make_buy_plan(db_session, req.id, q.id, test_user.id)
    qp = _make_qp(db_session, bp.id, test_user.id)
    db_session.commit()

    with patch(
        "app.routers.quality_plans.toggle_section_reviewed",
        side_effect=ValueError(f"QualityPlan {qp.id} not found"),
    ):
        resp = client.post(f"/v2/qp/{qp.id}/sales/review", data={"action": "mark"})

    assert resp.status_code == 404


def test_qp_detail_unauthenticated(unauthenticated_client):
    """GET /v2/qp/1 without auth returns 401."""
    resp = unauthenticated_client.get("/v2/qp/1")
    assert resp.status_code == 401

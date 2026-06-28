"""tests/test_nightly_qp_coverage.py — Nightly coverage boost for
app/routers/quality_plans.py.

Targets the uncovered helper functions (_coerce, _parse_date, _section_approved) and the
error/exception paths in the submit and section-gate route handlers.

Called by: pytest (nightly coverage run) Depends on: conftest (db_session, client,
test_user), test_c2a_gates helpers
"""

import os
import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ.setdefault("TESTING", "1")

from app.constants import BuyPlanStatus
from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.quality_plan import QpFruLookup, QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.routers.quality_plans import _coerce, _parse_date, _section_approved

# ── Pure-function unit tests ──────────────────────────────────────────────────


class TestCoerce:
    """_coerce: type coercion for HTML form values."""

    def test_none_input_returns_none(self):
        assert _coerce("text", None) is None

    def test_blank_strip_returns_none(self):
        assert _coerce("text", "   ") is None

    def test_bool_true_variants(self):
        for val in ("true", "True", "TRUE", "yes", "1", "on"):
            assert _coerce("bool", val) is True, f"Expected True for {val!r}"

    def test_bool_false_variants(self):
        for val in ("false", "False", "FALSE", "no", "0", "off"):
            assert _coerce("bool", val) is False, f"Expected False for {val!r}"

    def test_bool_junk_returns_none(self):
        assert _coerce("bool", "maybe") is None

    def test_int_valid(self):
        assert _coerce("int", "42") == 42

    def test_int_invalid_returns_none(self):
        assert _coerce("int", "abc") is None

    def test_text_passthrough(self):
        assert _coerce("text", "  hello  ") == "hello"


class TestParseDate:
    """_parse_date: parse YYYY-MM-DD HTML date inputs."""

    def test_none_returns_none(self):
        assert _parse_date(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_date("") is None

    def test_whitespace_returns_none(self):
        assert _parse_date("   ") is None

    def test_valid_date(self):
        assert _parse_date("2025-03-15") == date(2025, 3, 15)

    def test_invalid_date_returns_none(self):
        assert _parse_date("not-a-date") is None

    def test_partial_date_returns_none(self):
        assert _parse_date("2025-13-99") is None


class TestSectionApproved:
    """_section_approved: checks whether a section already carries an approved stamp."""

    def _qp_mock(self, sales_approved=False, purchasing_approved=False):
        from unittest.mock import MagicMock

        qp = MagicMock(spec=QualityPlan)
        qp.sales_section_approved_at = datetime.now(timezone.utc) if sales_approved else None
        qp.purchasing_section_approved_at = datetime.now(timezone.utc) if purchasing_approved else None
        return qp

    def test_sales_order_not_approved(self):
        qp = self._qp_mock(sales_approved=False)
        assert _section_approved(qp, "qp_sales") is False

    def test_sales_order_approved(self):
        qp = self._qp_mock(sales_approved=True)
        assert _section_approved(qp, "qp_sales") is True

    def test_purchase_order_not_approved(self):
        qp = self._qp_mock(purchasing_approved=False)
        assert _section_approved(qp, "purchase_order") is False

    def test_purchase_order_approved(self):
        qp = self._qp_mock(purchasing_approved=True)
        assert _section_approved(qp, "purchase_order") is True


# ── Helpers (mirrors test_c2a_gates._make_qp) ─────────────────────────────────


def _make_admin_user(db: Session) -> User:
    u = User(
        email=f"nqp-{uuid.uuid4().hex[:8]}@test.com",
        name="NQP Admin",
        role="admin",
        azure_id=f"azure-nqp-{uuid.uuid4().hex[:8]}",
        is_active=True,
        can_approve_qp_sales=True,
        can_approve_pos=True,
    )
    db.add(u)
    db.flush()
    return u


def _make_qp(db: Session, owner: User) -> QualityPlan:
    req = Requisition(
        name=f"REQ-NQP-{uuid.uuid4().hex[:6]}",
        customer_name="NQPCo",
        status="active",
        created_by=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()

    quote = Quote(
        requisition_id=req.id,
        quote_number=f"QNQP-{uuid.uuid4().hex[:8]}",
        line_items=[],
        status="sent",
        created_by_id=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(quote)
    db.flush()

    bp = BuyPlan(
        requisition_id=req.id,
        quote_id=quote.id,
        status=BuyPlanStatus.DRAFT.value,
        so_status="pending",
        total_cost=500.0,
    )
    db.add(bp)
    db.flush()

    qp = QualityPlan(
        buy_plan_id=bp.id,
        created_by_id=owner.id,
        order_type="new",
        status="draft",
        sales_condition="New",
        sales_quantity=5,
        sales_product_commodity="SSD",
        sales_testing_required=True,
        purchasing_po_number="PO-NQP-01",
        purchasing_condition="New",
        purchasing_product_commodity="SSD",
        purchasing_testing_required=True,
    )
    db.add(qp)
    db.flush()
    return qp


@pytest.fixture()
def _qp_client(db_session: Session):
    """TestClient + (user, qp) for quality-plan route tests."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    user = _make_admin_user(db_session)
    qp = _make_qp(db_session, user)
    db_session.commit()

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: user
    try:
        yield TestClient(app, raise_server_exceptions=False), user, qp
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


# ── Route handler error-path tests ────────────────────────────────────────────


class TestQpRouteErrorPaths:
    def test_submit_qp_not_found_returns_404(self, _qp_client):
        client, _user, _qp = _qp_client
        r = client.post("/v2/qp/999999/submit")
        assert r.status_code == 404

    def test_submit_sales_not_found_returns_404(self, _qp_client):
        client, _user, _qp = _qp_client
        r = client.post("/v2/qp/999999/submit-sales")
        assert r.status_code == 404

    def test_submit_purchasing_not_found_returns_404(self, _qp_client):
        client, _user, _qp = _qp_client
        r = client.post("/v2/qp/999999/submit-purchasing")
        assert r.status_code == 404

    def test_submit_qp_incomplete_returns_200_with_errors(self, _qp_client, db_session: Session):
        """An incomplete QP hits the IncompleteQPError path → 200 (re-render with
        errors)."""
        client, _user, qp = _qp_client
        # Blank the canonical Sales Order # (now on the linked BuyPlan) so submit() hits
        # the IncompleteQPError path → 200 re-render with errors.
        from app.models.buy_plan import BuyPlan

        bp = db_session.get(BuyPlan, qp.buy_plan_id)
        bp.sales_order_number = None
        db_session.commit()
        r = client.post(f"/v2/qp/{qp.id}/submit")
        assert r.status_code == 200

    def test_patch_sales_not_found_returns_404(self, _qp_client):
        client, _user, _qp = _qp_client
        r = client.patch("/v2/qp/999999/sales", data={"sales_so_number": "TSO-X"})
        assert r.status_code == 404

    def test_patch_purchasing_not_found_returns_404(self, _qp_client):
        client, _user, _qp = _qp_client
        r = client.patch("/v2/qp/999999/purchasing", data={"purchasing_po_number": "PO-X"})
        assert r.status_code == 404

    def test_patch_sales_when_not_approved_writes_field(self, _qp_client, db_session: Session):
        """PATCH /v2/qp/{id}/sales on an unapproved section updates the field."""
        client, _user, qp = _qp_client
        r = client.patch(f"/v2/qp/{qp.id}/sales", data={"sales_condition": "Refurbished"})
        assert r.status_code == 200
        db_session.refresh(qp)
        assert qp.sales_condition == "Refurbished"

    def test_patch_purchasing_when_not_approved_writes_field(self, _qp_client, db_session: Session):
        """PATCH /v2/qp/{id}/purchasing on an unapproved section updates the field."""
        client, _user, qp = _qp_client
        r = client.patch(f"/v2/qp/{qp.id}/purchasing", data={"purchasing_po_number": "PO-NEW-77"})
        assert r.status_code == 200
        db_session.refresh(qp)
        assert qp.purchasing_po_number == "PO-NEW-77"

    def test_patch_sales_when_already_approved_is_noop(self, _qp_client, db_session: Session):
        """PATCH on an approved section must not overwrite data."""
        client, _user, qp = _qp_client
        qp.sales_section_approved_at = datetime.now(timezone.utc)
        db_session.commit()
        r = client.patch(f"/v2/qp/{qp.id}/sales", data={"sales_condition": "IGNORED"})
        assert r.status_code == 200
        db_session.refresh(qp)
        assert qp.sales_condition == "New"

    def test_add_serial_not_found_returns_404(self, _qp_client):
        client, _user, _qp = _qp_client
        r = client.post("/v2/qp/999999/serial", data={"serial_number": "SN-001"})
        assert r.status_code == 404

    def test_delete_serial_not_found_returns_404(self, _qp_client):
        client, _user, _qp = _qp_client
        r = client.delete("/v2/qp/999999/serial/1")
        assert r.status_code == 404

    def test_add_fru_not_found_returns_404(self, _qp_client):
        client, _user, _qp = _qp_client
        r = client.post("/v2/qp/999999/fru", data={"fru": "LM317T"})
        assert r.status_code == 404

    def test_delete_fru_not_found_returns_404(self, _qp_client):
        client, _user, _qp = _qp_client
        r = client.delete("/v2/qp/999999/fru/1")
        assert r.status_code == 404

    def test_delete_fru_wrong_qp_returns_404(self, _qp_client, db_session: Session):
        """Deleting a FRU lookup that belongs to a different QP returns 404."""
        client, _user, qp = _qp_client
        # Create a lookup under a different QP id
        other_user = _make_admin_user(db_session)
        other_qp = _make_qp(db_session, other_user)
        db_session.commit()

        fru = QpFruLookup(qp_id=other_qp.id, fru_norm="xc7a35t")
        db_session.add(fru)
        db_session.commit()

        r = client.delete(f"/v2/qp/{qp.id}/fru/{fru.id}")
        assert r.status_code == 404

    def test_load_qp_for_edit_not_found_returns_404(self, _qp_client):
        """_load_qp_for_edit raises 404 when no row exists."""
        client, _user, _qp = _qp_client
        r = client.patch("/v2/qp/888888/sales", data={})
        assert r.status_code == 404

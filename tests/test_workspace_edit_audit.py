"""test_workspace_edit_audit.py — stale guard + field-diff audit on EVERY edit route
(Approvals Workspace 2.1).

For each edit route (/so-number, /lines/add, /lines/{id}/edit, /lines/{id}/remove,
/lines/bulk, confirm-po, the NEW qp-sales) this asserts the three 2.1 invariants:
  - a real edit lands exactly ONE ActivityType.FIELD_EDIT row with the correct
    field/old/new (bulk batches every touched line into that one row);
  - a stale expected_updated_at token → 409 (HX-Reswap: none) and NO write, NO row;
  - a no-change save writes NO row.
Plus the unified can_edit_plan matrix (draft/pending → owner or manager;
active → manager only; terminal locked) and the Deal Sheet /header + submit contracts.

Called by: pytest
Depends on: conftest (db_session, test_user), tests.test_approvals_hub_tabs builders,
            app.routers.htmx.{buy_plans,approvals_hub}, app.services.field_audit.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import (
    ActivityType,
    BuyPlanLineStatus,
    BuyPlanStatus,
    OfferStatus,
    UserRole,
)
from app.database import get_db
from app.dependencies import require_buyplan_approver, require_buyplan_po_approver, require_user
from app.models import ActivityLog, Offer, User
from app.models.buy_plan import BuyPlan, BuyPlanLine
from app.models.quality_plan import QualityPlan
from app.models.vendors import VendorCard
from tests.test_approvals_hub_tabs import _line, _plan, _req_quote

STALE = "2020-01-01T00:00:00+00:00"  # a token that can never match a live updated_at


@pytest.fixture()
def hub_client(db_session: Session, test_user: User):
    """TestClient authed as test_user (buyer, plan owner via _req_quote)."""
    from app.main import app

    app.dependency_overrides[get_db] = lambda: (yield db_session)  # type: ignore[misc]
    app.dependency_overrides[require_user] = lambda: test_user
    app.dependency_overrides[require_buyplan_approver] = lambda: test_user
    app.dependency_overrides[require_buyplan_po_approver] = lambda: test_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (get_db, require_user, require_buyplan_approver, require_buyplan_po_approver):
            app.dependency_overrides.pop(dep, None)


def _field_edit_rows(db: Session) -> list[ActivityLog]:
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.activity_type == ActivityType.FIELD_EDIT.value)
        .order_by(ActivityLog.id)
        .all()
    )


def _attachable_offer(db: Session, req, rq, *, vendor: str = "SwapVendor", price: float = 2.5) -> Offer:
    """An ACTIVE offer fully attachable to (req, rq) — requisition_id set, so
    _ensure_offer_attachable accepts it."""
    vc = VendorCard(normalized_name=f"vc-{uuid.uuid4().hex[:8]}", display_name=vendor)
    db.add(vc)
    db.flush()
    off = Offer(
        requisition_id=req.id,
        requirement_id=rq.id,
        vendor_card_id=vc.id,
        vendor_name=vendor,
        vendor_name_normalized=vendor.lower(),
        mpn="LM317",
        normalized_mpn="LM317",
        unit_price=price,
        status=OfferStatus.ACTIVE.value,
    )
    db.add(off)
    db.flush()
    return off


def _draft_plan_with_line(db: Session, user: User):
    req, q, rq = _req_quote(db, user)
    bp = _plan(db, req, q, status=BuyPlanStatus.DRAFT.value)
    line = _line(db, bp, rq, user, status=BuyPlanLineStatus.AWAITING_PO.value)
    db.commit()
    return req, rq, bp, line


# ── /so-number ───────────────────────────────────────────────────────────


# The /so-number, /lines/add, and /lines/{id}/remove routes retired with the legacy
# detail page (Deal Sheet T3b). Their audit invariants live on with their successors:
# SO# via POST /v2/partials/approvals/plan/{id}/header (TestHeaderEndpoint below) and
# add/remove via /lines/bulk (TestBulkAudit below — adds audit "line added", omission
# audits "line removed", stale 409s write nothing).


# ── /lines/{id}/edit ─────────────────────────────────────────────────────


class TestEditLineAudit:
    def test_quantity_edit_writes_row(self, hub_client, db_session, test_user):
        _req, _rq, bp, line = _draft_plan_with_line(db_session, test_user)

        r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit", data={"quantity": "150"})
        assert r.status_code == 200
        rows = _field_edit_rows(db_session)
        assert len(rows) == 1
        assert rows[0].buy_plan_line_id == line.id
        assert rows[0].details["edits"] == [{"field": "quantity", "old": "100", "new": "150"}]

    def test_vendor_swap_logs_vendor_names(self, hub_client, db_session, test_user):
        req, rq, bp, line = _draft_plan_with_line(db_session, test_user)
        new_off = _attachable_offer(db_session, req, rq)
        db_session.commit()

        r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit", data={"offer_id": new_off.id})
        assert r.status_code == 200
        rows = _field_edit_rows(db_session)
        assert len(rows) == 1
        vendor_edit = next(e for e in rows[0].details["edits"] if e["field"] == "vendor")
        assert vendor_edit["old"] == "Acme Dist"
        assert vendor_edit["new"] == "SwapVendor"

    def test_no_change_resend_writes_no_row(self, hub_client, db_session, test_user):
        _req, _rq, bp, line = _draft_plan_with_line(db_session, test_user)

        r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit", data={"quantity": "100"})
        assert r.status_code == 200
        assert _field_edit_rows(db_session) == []

    def test_stale_token_409s_without_writing(self, hub_client, db_session, test_user):
        _req, _rq, bp, line = _draft_plan_with_line(db_session, test_user)

        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/edit",
            data={"quantity": "150", "expected_updated_at": STALE},
        )
        assert r.status_code == 409
        db_session.expire_all()
        assert line.quantity == 100
        assert _field_edit_rows(db_session) == []


# ── /lines/bulk ──────────────────────────────────────────────────────────


class TestBulkAudit:
    def test_two_line_save_batches_into_one_row(self, hub_client, db_session, test_user):
        req, rq, bp, line1 = _draft_plan_with_line(db_session, test_user)
        line2 = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
        db_session.commit()

        payload = {
            "lines": [
                {"line_id": line1.id, "quantity": 111},
                {"line_id": line2.id, "quantity": 222},
            ],
            "known_line_ids": [line1.id, line2.id],
        }
        r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/lines/bulk", data={"payload": json.dumps(payload)})
        assert r.status_code == 200
        rows = _field_edit_rows(db_session)
        assert len(rows) == 1  # ONE row per save, both lines batched
        edits = rows[0].details["edits"]
        assert {(e["field"], e["line_id"]) for e in edits} == {("quantity", line1.id), ("quantity", line2.id)}
        assert rows[0].buy_plan_line_id is None  # multi-line row attributes per edit

    def test_no_change_save_writes_no_row(self, hub_client, db_session, test_user):
        _req, _rq, bp, line = _draft_plan_with_line(db_session, test_user)
        payload = {"lines": [{"line_id": line.id, "quantity": 100}], "known_line_ids": [line.id]}

        r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/lines/bulk", data={"payload": json.dumps(payload)})
        assert r.status_code == 200
        assert _field_edit_rows(db_session) == []

    def test_stale_token_409s_without_writing(self, hub_client, db_session, test_user):
        _req, _rq, bp, line = _draft_plan_with_line(db_session, test_user)
        payload = {"lines": [{"line_id": line.id, "quantity": 999}], "known_line_ids": [line.id]}

        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/bulk",
            data={"payload": json.dumps(payload), "expected_updated_at": STALE},
        )
        assert r.status_code == 409
        db_session.expire_all()
        assert line.quantity == 100
        assert _field_edit_rows(db_session) == []

    def test_add_via_bulk_writes_line_added_row(self, hub_client, db_session, test_user):
        req, rq, bp, line1 = _draft_plan_with_line(db_session, test_user)
        off = _attachable_offer(db_session, req, rq)
        db_session.commit()

        payload = {
            "lines": [
                {"line_id": line1.id},
                {"requirement_id": rq.id, "offer_id": off.id, "quantity": 25, "unit_sell": 5.0},
            ],
            "known_line_ids": [line1.id],
        }
        r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/lines/bulk", data={"payload": json.dumps(payload)})
        assert r.status_code == 200
        rows = _field_edit_rows(db_session)
        assert len(rows) == 1
        (edit,) = rows[0].details["edits"]
        assert edit["field"] == "line added"
        assert "SwapVendor" in edit["new"] and "×25" in edit["new"]
        new_line = db_session.query(BuyPlanLine).filter(BuyPlanLine.offer_id == off.id).one()
        assert edit["line_id"] == new_line.id

    def test_removal_by_omission_logs_in_same_row(self, hub_client, db_session, test_user):
        req, rq, bp, line1 = _draft_plan_with_line(db_session, test_user)
        line2 = _line(db_session, bp, rq, test_user, status=BuyPlanLineStatus.AWAITING_PO.value)
        db_session.commit()
        line2_id = line2.id

        payload = {
            "lines": [{"line_id": line1.id, "quantity": 111}],
            "known_line_ids": [line1.id, line2_id],
        }
        r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/lines/bulk", data={"payload": json.dumps(payload)})
        assert r.status_code == 200
        rows = _field_edit_rows(db_session)
        assert len(rows) == 1
        fields = {e["field"] for e in rows[0].details["edits"]}
        assert fields == {"quantity", "line removed"}
        assert db_session.get(BuyPlanLine, line2_id) is None


# ── confirm-po (line fields + QP-purchasing merged into ONE row) ─────────


class TestConfirmPoAudit:
    def _active_plan_awaiting_line(self, db: Session, user: User):
        req, q, rq = _req_quote(db, user)
        bp = _plan(db, req, q, status=BuyPlanStatus.ACTIVE.value)
        line = _line(db, bp, rq, user, status=BuyPlanLineStatus.AWAITING_PO.value)
        db.commit()
        return bp, line

    def test_confirm_po_one_row_with_line_and_qp_edits(self, hub_client, db_session, test_user):
        bp, line = self._active_plan_awaiting_line(db_session, test_user)

        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/confirm-po",
            data={
                "po_number": "PO-501",
                "estimated_ship_date": "2026-08-01",
                "payment_method": "wire",
                "qp_purchasing_condition": "NEW",
            },
        )
        assert r.status_code == 200
        rows = _field_edit_rows(db_session)
        assert len(rows) == 1  # line fields + QP answers merged — one row per save
        assert rows[0].buy_plan_line_id == line.id
        by_field = {e["field"]: e for e in rows[0].details["edits"]}
        assert by_field["po_number"]["new"] == "PO-501"
        assert by_field["payment_method"]["new"] == "wire"
        assert by_field["purchasing_condition"]["new"] == "NEW"
        assert "estimated_ship_date" in by_field

    def test_stale_token_409s_without_writing(self, hub_client, db_session, test_user):
        bp, line = self._active_plan_awaiting_line(db_session, test_user)

        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/lines/{line.id}/confirm-po",
            data={"po_number": "PO-501", "expected_updated_at": STALE},
        )
        assert r.status_code == 409
        db_session.expire_all()
        assert line.status == BuyPlanLineStatus.AWAITING_PO.value
        assert line.po_number is None
        assert _field_edit_rows(db_session) == []


# ── qp-sales (NEW route) ─────────────────────────────────────────────────


class TestQpSales:
    def test_save_creates_qp_row_and_audits(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)

        r = hub_client.post(
            f"/v2/partials/approvals/plan/{bp.id}/qp-sales",
            data={"qp_sales_condition": "NEW SEALED", "qp_sales_testing_required": "yes"},
        )
        assert r.status_code == 200
        assert r.headers.get("HX-Trigger") == "awListRefresh"
        assert "NEW SEALED" in r.text  # the re-rendered pane shows the saved answer
        qp = db_session.query(QualityPlan).filter(QualityPlan.buy_plan_id == bp.id).one()
        assert qp.sales_condition == "NEW SEALED"
        assert qp.sales_testing_required is True
        rows = _field_edit_rows(db_session)
        assert len(rows) == 1
        by_field = {e["field"]: e for e in rows[0].details["edits"]}
        assert by_field["sales_condition"] == {"field": "sales_condition", "old": "", "new": "NEW SEALED"}
        assert by_field["sales_testing_required"]["new"] == "yes"

    def test_no_change_save_writes_no_row(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)
        db_session.add(QualityPlan(buy_plan_id=bp.id, created_by_id=test_user.id, sales_condition="NEW SEALED"))
        db_session.commit()

        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/qp-sales", data={"qp_sales_condition": "NEW SEALED"})
        assert r.status_code == 200
        assert _field_edit_rows(db_session) == []

    def test_stale_token_409s_without_writing(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)
        qp = QualityPlan(buy_plan_id=bp.id, created_by_id=test_user.id, sales_condition="OLD")
        qp.updated_at = datetime.now(UTC)
        db_session.add(qp)
        db_session.commit()

        r = hub_client.post(
            f"/v2/partials/approvals/plan/{bp.id}/qp-sales",
            data={"qp_sales_condition": "CHANGED", "expected_updated_at": STALE},
        )
        assert r.status_code == 409
        db_session.expire_all()
        assert qp.sales_condition == "OLD"
        assert _field_edit_rows(db_session) == []

    def test_pending_plan_owner_and_manager_allowed(self, hub_client, db_session, test_user):
        """Deal Sheet matrix (2026-08-18): pending → the OWNING salesperson OR a manager
        may edit QP-sales (the old pending-manager-only rule was deliberately widened
        when can_edit_qp_sales was unified onto can_edit_plan)."""
        req, q, _rq = _req_quote(db_session, test_user)
        bp = _plan(db_session, req, q, status=BuyPlanStatus.PENDING.value)
        db_session.commit()

        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/qp-sales", data={"qp_sales_condition": "X"})
        assert r.status_code == 200  # owner allowed at pending

        test_user.role = UserRole.MANAGER.value
        db_session.commit()
        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/qp-sales", data={"qp_sales_condition": "Y"})
        assert r.status_code == 200

    def test_active_plan_manager_only(self, hub_client, db_session, test_user):
        """Deal Sheet matrix: active → manager/admin only; the owning non-manager
        is refused (audited edits stay open to managers through active)."""
        req, q, _rq = _req_quote(db_session, test_user)
        bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
        db_session.commit()

        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/qp-sales", data={"qp_sales_condition": "X"})
        assert r.status_code == 403  # owner (non-manager) locked at active

        test_user.role = UserRole.MANAGER.value
        db_session.commit()
        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/qp-sales", data={"qp_sales_condition": "X"})
        assert r.status_code == 200

    def test_pane_shows_editor_only_when_editable(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)

        body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
        assert f"/v2/partials/approvals/plan/{bp.id}/qp-sales" in body  # editable draft → form

        bp.status = BuyPlanStatus.ACTIVE.value
        db_session.commit()
        body = hub_client.get(f"/v2/partials/approvals/plan/{bp.id}/pane").text
        assert f"/v2/partials/approvals/plan/{bp.id}/qp-sales" not in body  # locked → no form


# ── /plan/{id}/header — the Deal Sheet header writer ─────────────────────


class TestHeaderEndpoint:
    """POST /v2/partials/approvals/plan/{id}/header — sparse, stale-guarded, audited,
    gated by can_edit_plan (Deal Sheet T1 contract)."""

    def test_single_field_save_persists_and_audits_once(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)

        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/header", data={"sales_order_number": "SO-500"})
        assert r.status_code == 200
        db_session.expire_all()
        bp2 = db_session.get(BuyPlan, bp.id)
        assert bp2.sales_order_number == "SO-500"
        rows = _field_edit_rows(db_session)
        assert len(rows) == 1
        assert rows[0].details["edits"] == [{"field": "sales_order_number", "old": "", "new": "SO-500"}]

    def test_sparse_semantics_absent_fields_untouched(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)
        bp.customer_po_number = "CPO-1"
        bp.salesperson_notes = "keep me"
        db_session.commit()

        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/header", data={"sales_order_number": "SO-501"})
        assert r.status_code == 200
        db_session.expire_all()
        bp2 = db_session.get(BuyPlan, bp.id)
        assert bp2.customer_po_number == "CPO-1"  # absent field never touched
        assert bp2.salesperson_notes == "keep me"

    def test_no_change_save_writes_no_row(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)
        bp.sales_order_number = "SO-77"
        db_session.commit()

        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/header", data={"sales_order_number": "SO-77"})
        assert r.status_code == 200
        assert _field_edit_rows(db_session) == []

    def test_provided_blank_is_explicit_clear(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)
        bp.customer_po_number = "CPO-2"
        db_session.commit()

        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/header", data={"customer_po_number": ""})
        assert r.status_code == 200
        db_session.expire_all()
        assert db_session.get(BuyPlan, bp.id).customer_po_number is None

    def test_stale_token_conflict_409_no_write(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)

        r = hub_client.post(
            f"/v2/partials/approvals/plan/{bp.id}/header",
            data={"sales_order_number": "SO-503", "expected_updated_at": "2000-01-01T00:00:00+00:00"},
        )
        assert r.status_code == 409
        db_session.expire_all()
        assert db_session.get(BuyPlan, bp.id).sales_order_number is None

    def test_active_plan_owner_403_manager_200(self, hub_client, db_session, test_user):
        req, q, _rq = _req_quote(db_session, test_user)
        bp = _plan(db_session, req, q, status=BuyPlanStatus.ACTIVE.value)
        db_session.commit()

        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/header", data={"sales_order_number": "SO-504"})
        assert r.status_code == 403  # owner (non-manager) locked at active

        test_user.role = UserRole.MANAGER.value
        db_session.commit()
        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/header", data={"sales_order_number": "SO-504"})
        assert r.status_code == 200

    def test_no_field_provided_400(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)
        r = hub_client.post(f"/v2/partials/approvals/plan/{bp.id}/header", data={"lens": "sales-orders"})
        assert r.status_code == 400


class TestSubmitContract:
    """Deal Sheet submit: thin action validating the RECORD; blank never clobbers."""

    def test_submit_reads_so_from_record(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)
        bp.sales_order_number = "SO-REC-1"
        db_session.commit()

        # No form fields at all — the record already carries the SO#.
        r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/submit", data={})
        assert r.status_code == 200
        db_session.expire_all()
        assert db_session.get(BuyPlan, bp.id).status == BuyPlanStatus.PENDING.value

    def test_submit_without_so_anywhere_400(self, hub_client, db_session, test_user):
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)
        r = hub_client.post(f"/v2/partials/buy-plans/{bp.id}/submit", data={})
        assert r.status_code == 400
        assert "Sales Order #" in r.text

    def test_blank_resubmit_never_clobbers(self, hub_client, db_session, test_user):
        """Regression: the old submit assigned customer_po/notes unconditionally,
        clearing them on any resubmit that left the modal fields blank."""
        _req, _rq, bp, _line_ = _draft_plan_with_line(db_session, test_user)
        bp.sales_order_number = "SO-REC-2"
        bp.customer_po_number = "CPO-KEEP"
        bp.salesperson_notes = "notes-keep"
        db_session.commit()

        r = hub_client.post(
            f"/v2/partials/buy-plans/{bp.id}/submit",
            data={"sales_order_number": "", "customer_po_number": "", "salesperson_notes": ""},
        )
        assert r.status_code == 200
        db_session.expire_all()
        bp2 = db_session.get(BuyPlan, bp.id)
        assert bp2.customer_po_number == "CPO-KEEP"
        assert bp2.salesperson_notes == "notes-keep"
        assert bp2.sales_order_number == "SO-REC-2"

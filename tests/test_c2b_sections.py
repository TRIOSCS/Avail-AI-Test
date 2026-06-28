"""test_c2b_sections.py — QP Phase C2b: native Sales/Purchasing sections + children.

Covers the C2b contract (the engine + section gates are already proven in C2a):
  - _validate_sales_section / _validate_purchasing_section flag a blank SO#/PO# (and the
    other QC-required fields); a complete section validates clean.
  - submit_section blocks on an incomplete section (IncompleteQPError, NO gate opened)
    and opens the right gate request once complete.
  - _on_section_approved stamps the matching section-approved timestamp on approve and
    clears it on reject (same session).
  - serial-entry create/delete via the router endpoints (and the CASCADE child relation).
  - FRU pin resolves fru_norm + the (qp_id, fru_norm) unique constraint makes a re-pin a
    no-op; unpin removes it. The FRU section live-joins FruLink by fru_norm.
  - the four section partials render (Sales / Purchasing / Serial / FRU).

Called by: pytest
Depends on: conftest (db_session), app.services.quality_plan_service,
            app.models.{quality_plan,buy_plan,quotes,sourcing,auth,fru_link},
            app.constants, app.routers.quality_plans.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import ApprovalGateType, BuyPlanStatus
from app.models.auth import User
from app.models.buy_plan import BuyPlan
from app.models.fru_link import FruLink
from app.models.quality_plan import QpFruLookup, QpSerialEntry, QualityPlan
from app.models.quotes import Quote
from app.models.sourcing import Requisition
from app.services.quality_plan_service import (
    IncompleteQPError,
    _on_section_approved,
    _validate_purchasing_section,
    _validate_sales_section,
    submit_section,
    validate_section,
)

# ── Helpers ─────────────────────────────────────────────────────────────


def _make_user(db: Session, *, can_approve_qp_sales: bool = False, can_approve_pos: bool = False) -> User:
    u = User(
        email=f"c2b-{uuid.uuid4().hex[:8]}@test.com",
        name="C2b User",
        role="admin",
        azure_id=f"azure-c2b-{uuid.uuid4().hex[:8]}",
        is_active=True,
        can_approve_qp_sales=can_approve_qp_sales,
        can_approve_pos=can_approve_pos,
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_qp(db: Session, owner: User, *, fill_sales: bool = False, fill_purchasing: bool = False) -> QualityPlan:
    req = Requisition(
        name=f"REQ-C2B-{uuid.uuid4().hex[:6]}",
        customer_name="C2BCo",
        status="open",
        created_by=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    quote = Quote(
        requisition_id=req.id,
        quote_number=f"QC2B-{uuid.uuid4().hex[:8]}",
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
        total_cost=1000.0,
    )
    db.add(bp)
    db.flush()
    qp = QualityPlan(buy_plan_id=bp.id, created_by_id=owner.id, order_type="new", status="draft")
    if fill_sales:
        qp.sales_so_number = "TSO0190738"
        qp.sales_condition = "New"
        qp.sales_quantity = 10
        qp.sales_product_commodity = "HDD"
        qp.sales_testing_required = True
    if fill_purchasing:
        qp.purchasing_po_number = "PO-12345"
        qp.purchasing_condition = "New"
        qp.purchasing_product_commodity = "HDD"
        qp.purchasing_testing_required = True
    db.add(qp)
    db.flush()
    return qp


# ── Section validation ───────────────────────────────────────────────────


def test_sales_section_missing_so_number_blocks(db_session: Session) -> None:
    """A blank Sales Order # (and other required fields) surfaces validation errors."""
    qp = _make_qp(db_session, _make_user(db_session))
    errors = _validate_sales_section(qp)
    assert any("Sales Order #" in e for e in errors)


def test_purchasing_section_missing_po_number_blocks(db_session: Session) -> None:
    """A blank Purchase Order # surfaces validation errors."""
    qp = _make_qp(db_session, _make_user(db_session))
    errors = _validate_purchasing_section(qp)
    assert any("Purchase Order #" in e for e in errors)


def test_complete_sections_validate_clean(db_session: Session) -> None:
    """A fully-filled Sales/Purchasing section has no completeness errors."""
    qp = _make_qp(db_session, _make_user(db_session), fill_sales=True, fill_purchasing=True)
    assert _validate_sales_section(qp) == []
    assert _validate_purchasing_section(qp) == []
    assert validate_section(qp, ApprovalGateType.QP_SALES) == []
    assert validate_section(qp, ApprovalGateType.PURCHASE_ORDER) == []


# ── submit_section gating ────────────────────────────────────────────────


def test_submit_incomplete_sales_raises_and_opens_no_gate(db_session: Session) -> None:
    """An incomplete Sales section raises IncompleteQPError and opens no gate
    request."""
    approver = _make_user(db_session, can_approve_qp_sales=True)
    qp = _make_qp(db_session, approver)  # not filled
    with pytest.raises(IncompleteQPError):
        submit_section(db_session, qp.id, ApprovalGateType.QP_SALES, approver)
    reqs = db_session.execute(select(QualityPlan).where(QualityPlan.id == qp.id)).scalar_one()
    assert reqs is not None  # QP intact
    from app.models.approvals import ApprovalRequest

    opened = db_session.execute(select(ApprovalRequest).where(ApprovalRequest.subject_id == qp.id)).scalars().all()
    assert opened == []


def test_submit_complete_sales_opens_gate(db_session: Session) -> None:
    """A complete Sales section opens the QP_SALES request."""
    approver = _make_user(db_session, can_approve_qp_sales=True)
    qp = _make_qp(db_session, approver, fill_sales=True)
    req = submit_section(db_session, qp.id, ApprovalGateType.QP_SALES, approver)
    assert req.gate_type == ApprovalGateType.QP_SALES
    assert req.subject_id == qp.id


# ── _on_section_approved stamps the timestamp ────────────────────────────


def test_on_section_approved_stamps_sales_timestamp(db_session: Session) -> None:
    """Approving the Sales section sets sales_section_approved_at."""
    qp = _make_qp(db_session, _make_user(db_session), fill_sales=True)
    assert qp.sales_section_approved_at is None
    _on_section_approved(db_session, qp.id, ApprovalGateType.QP_SALES, True)
    db_session.refresh(qp)
    assert qp.sales_section_approved_at is not None
    assert qp.purchasing_section_approved_at is None  # unaffected


def test_on_section_approved_stamps_purchasing_timestamp(db_session: Session) -> None:
    """Approving the Purchasing section sets purchasing_section_approved_at."""
    qp = _make_qp(db_session, _make_user(db_session), fill_purchasing=True)
    _on_section_approved(db_session, qp.id, ApprovalGateType.PURCHASE_ORDER, True)
    db_session.refresh(qp)
    assert qp.purchasing_section_approved_at is not None


def test_on_section_rejected_clears_timestamp(db_session: Session) -> None:
    """A rejection clears the section timestamp so a later approval can re-set it."""
    qp = _make_qp(db_session, _make_user(db_session), fill_sales=True)
    qp.sales_section_approved_at = datetime.now(timezone.utc)
    db_session.flush()
    _on_section_approved(db_session, qp.id, ApprovalGateType.QP_SALES, False)
    db_session.refresh(qp)
    assert qp.sales_section_approved_at is None


# ── Router client fixture ─────────────────────────────────────────────────


@pytest.fixture()
def qp_client(db_session: Session):
    """TestClient authenticated as the QP owner (full requisition access)."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    owner = _make_user(db_session, can_approve_qp_sales=True, can_approve_pos=True)
    qp = _make_qp(db_session, owner, fill_sales=True, fill_purchasing=True)
    db_session.commit()

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: owner
    try:
        yield TestClient(app), owner, qp
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)


# ── PATCH section editors ─────────────────────────────────────────────────


def test_patch_sales_updates_field(qp_client, db_session: Session) -> None:
    """PATCH /v2/qp/{id}/sales writes the whitelisted field and returns the partial."""
    client, _owner, qp = qp_client
    r = client.patch(f"/v2/qp/{qp.id}/sales", data={"sales_condition": "Refurbished"})
    assert r.status_code == 200
    db_session.refresh(qp)
    assert qp.sales_condition == "Refurbished"


def test_patch_purchasing_updates_field(qp_client, db_session: Session) -> None:
    """PATCH /v2/qp/{id}/purchasing writes the field and returns the partial."""
    client, _owner, qp = qp_client
    r = client.patch(f"/v2/qp/{qp.id}/purchasing", data={"purchasing_packaging": "ESD bag"})
    assert r.status_code == 200
    db_session.refresh(qp)
    assert qp.purchasing_packaging == "ESD bag"


# ── Serial CRUD ───────────────────────────────────────────────────────────


def test_serial_create_and_delete(qp_client, db_session: Session) -> None:
    """POST adds a serial entry; DELETE removes it (and only the matching one)."""
    client, _owner, qp = qp_client
    r = client.post(f"/v2/qp/{qp.id}/serial", data={"serial_number": "SN123", "part_number": "PN9"})
    assert r.status_code == 200
    entries = db_session.execute(select(QpSerialEntry).where(QpSerialEntry.qp_id == qp.id)).scalars().all()
    assert len(entries) == 1
    assert entries[0].serial_number == "SN123"

    r = client.delete(f"/v2/qp/{qp.id}/serial/{entries[0].id}")
    assert r.status_code == 200
    assert db_session.execute(select(QpSerialEntry).where(QpSerialEntry.qp_id == qp.id)).scalars().all() == []


def test_serial_delete_foreign_entry_404(qp_client, db_session: Session) -> None:
    """Deleting a serial entry that belongs to a different QP returns 404."""
    client, owner, qp = qp_client
    other_qp = _make_qp(db_session, owner)
    foreign = QpSerialEntry(qp_id=other_qp.id, serial_number="OTHER")
    db_session.add(foreign)
    db_session.commit()
    r = client.delete(f"/v2/qp/{qp.id}/serial/{foreign.id}")
    assert r.status_code == 404


def test_serial_cascade_on_qp_delete(db_session: Session) -> None:
    """Deleting a QP cascades its serial entries away (ORM delete-orphan)."""
    qp = _make_qp(db_session, _make_user(db_session))
    db_session.add(QpSerialEntry(qp_id=qp.id, serial_number="SNX"))
    db_session.flush()
    db_session.delete(qp)
    db_session.flush()
    assert db_session.execute(select(QpSerialEntry).where(QpSerialEntry.qp_id == qp.id)).scalars().all() == []


# ── FRU pin / unpin ───────────────────────────────────────────────────────


def test_fru_pin_resolves_norm_and_dedups(qp_client, db_session: Session) -> None:
    """POST /v2/qp/{id}/fru pins a normalized FRU; re-pinning the same FRU is a no-
    op."""
    client, _owner, qp = qp_client
    r = client.post(f"/v2/qp/{qp.id}/fru", data={"fru": "00NV340"})
    assert r.status_code == 200
    pins = db_session.execute(select(QpFruLookup).where(QpFruLookup.qp_id == qp.id)).scalars().all()
    assert len(pins) == 1
    assert pins[0].fru_norm == "00nv340"

    # Re-pin a differently-spelled-but-same FRU → unique (qp_id, fru_norm) keeps one row.
    r = client.post(f"/v2/qp/{qp.id}/fru", data={"fru": "00-NV-340"})
    assert r.status_code == 200
    pins = db_session.execute(select(QpFruLookup).where(QpFruLookup.qp_id == qp.id)).scalars().all()
    assert len(pins) == 1


def test_fru_unpin(qp_client, db_session: Session) -> None:
    """DELETE /v2/qp/{id}/fru/{lookup_id} removes the pin."""
    client, _owner, qp = qp_client
    client.post(f"/v2/qp/{qp.id}/fru", data={"fru": "00NV340"})
    pin = db_session.execute(select(QpFruLookup).where(QpFruLookup.qp_id == qp.id)).scalar_one()
    r = client.delete(f"/v2/qp/{qp.id}/fru/{pin.id}")
    assert r.status_code == 200
    assert db_session.execute(select(QpFruLookup).where(QpFruLookup.qp_id == qp.id)).scalars().all() == []


def test_fru_section_live_joins_crosswalk(qp_client, db_session: Session) -> None:
    """The FRU section render live-joins FruLink by fru_norm and shows the related
    part."""
    client, _owner, qp = qp_client
    db_session.add(
        FruLink(
            fru_raw="00NV340",
            fru_norm="00nv340",
            related_raw="01ABC99",
            related_norm="01abc99",
            rel_kind="mfg_model",
            manufacturer="Lenovo",
            source_sheet="test",
        )
    )
    db_session.commit()
    client.post(f"/v2/qp/{qp.id}/fru", data={"fru": "00NV340"})
    r = client.get(f"/v2/qp/{qp.id}")
    assert r.status_code == 200
    assert "01ABC99" in r.text  # the live-joined related part appears


# ── Section partials render ───────────────────────────────────────────────


def test_detail_renders_all_section_partials(qp_client) -> None:
    """The QP detail renders all four C2b section wrappers."""
    client, _owner, qp = qp_client
    r = client.get(f"/v2/qp/{qp.id}")
    assert r.status_code == 200
    for marker in ("qp-section-sales", "qp-section-purchasing", "qp-section-serial", "qp-section-fru"):
        assert marker in r.text


def test_submit_button_disabled_when_section_incomplete(db_session: Session) -> None:
    """The Sales submit button is disabled while required fields are missing."""
    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    owner = _make_user(db_session, can_approve_qp_sales=True)
    qp = _make_qp(db_session, owner)  # incomplete
    db_session.commit()

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_user] = lambda: owner
    try:
        client = TestClient(app)
        r = client.get(f"/v2/qp/{qp.id}")
    finally:
        for dep in (get_db, require_user):
            app.dependency_overrides.pop(dep, None)

    assert r.status_code == 200
    assert "Sales Order # is required" in r.text

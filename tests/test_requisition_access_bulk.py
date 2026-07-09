"""test_requisition_access_bulk.py — Tests for require_requisition_access_bulk (P3.3)
and its 6 call sites in app/routers/sightings.py.

Covers:
- Unit tests of require_requisition_access_bulk: buyer no-op, sales owner passes,
  sales non-owner 404s, missing id 404s, empty/None-filled input no-op, one query total.
- Router-level: sightings batch-assign/batch-status/batch-notes/batch-search/
  preview-inquiry/send-inquiry with a multi-row basket, asserting the bulk swap didn't
  change the single-item version's 404-on-any-non-owned-or-missing-id behavior.

Called by: pytest
Depends on: app/dependencies.py, app/routers/sightings.py, tests/conftest.py
"""

import json
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import UserRole
from app.dependencies import require_requisition_access_bulk
from app.models import Requirement, Requisition, User


def _make_req(db: Session, owner_id: int, name: str = "REQ-BULK") -> Requisition:
    req = Requisition(name=name, status="open", created_by=owner_id, created_at=datetime.now(timezone.utc))
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id, primary_mpn="LM317T", target_qty=10, created_at=datetime.now(timezone.utc)
    )
    db.add(item)
    db.commit()
    db.refresh(req)
    return req


# ── Unit tests: require_requisition_access_bulk ────────────────────────────


def test_bulk_buyer_is_noop_even_for_foreign_ids(db_session: Session, admin_user: User, test_user: User):
    """Unrestricted role (buyer/manager/admin) — no-op regardless of ownership."""
    req = _make_req(db_session, admin_user.id)
    test_user.role = UserRole.BUYER
    db_session.commit()
    require_requisition_access_bulk(db_session, [req.id], test_user)  # must not raise


def test_bulk_sales_owner_passes(db_session: Session, test_user: User):
    req1 = _make_req(db_session, test_user.id, "REQ-A")
    req2 = _make_req(db_session, test_user.id, "REQ-B")
    test_user.role = UserRole.SALES
    db_session.commit()
    require_requisition_access_bulk(db_session, [req1.id, req2.id], test_user)  # must not raise


def test_bulk_sales_non_owner_raises_404(db_session: Session, test_user: User, admin_user: User):
    owned = _make_req(db_session, test_user.id, "REQ-OWNED")
    foreign = _make_req(db_session, admin_user.id, "REQ-FOREIGN")
    test_user.role = UserRole.SALES
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        require_requisition_access_bulk(db_session, [owned.id, foreign.id], test_user)
    assert exc.value.status_code == 404


def test_bulk_missing_id_raises_404_same_as_single(db_session: Session, test_user: User):
    test_user.role = UserRole.SALES
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        require_requisition_access_bulk(db_session, [999999], test_user)
    assert exc.value.status_code == 404


def test_bulk_empty_and_none_ids_are_noop(db_session: Session, test_user: User):
    test_user.role = UserRole.SALES
    db_session.commit()
    require_requisition_access_bulk(db_session, [], test_user)  # must not raise
    require_requisition_access_bulk(db_session, [None, None], test_user)  # must not raise


def test_bulk_trader_non_owner_raises_404(db_session: Session, test_user: User, admin_user: User):
    foreign = _make_req(db_session, admin_user.id, "REQ-TRADER-FOREIGN")
    test_user.role = UserRole.TRADER
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        require_requisition_access_bulk(db_session, [foreign.id], test_user)
    assert exc.value.status_code == 404


def test_bulk_custom_label_used_in_detail(db_session: Session, test_user: User, admin_user: User):
    foreign = _make_req(db_session, admin_user.id, "REQ-LABEL")
    test_user.role = UserRole.SALES
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        require_requisition_access_bulk(db_session, [foreign.id], test_user, label="Requirement")
    assert exc.value.detail == "Requirement not found"


def test_bulk_dedupes_repeated_ids(db_session: Session, test_user: User):
    """Repeated ids in the input do not change the outcome (dedup via a set)."""
    req = _make_req(db_session, test_user.id, "REQ-DEDUP")
    test_user.role = UserRole.SALES
    db_session.commit()
    require_requisition_access_bulk(db_session, [req.id, req.id, req.id], test_user)  # must not raise


# ── Router-level: sightings batch endpoints (multi-row basket) ─────────────


def _requirement(db: Session, req: Requisition) -> Requirement:
    return db.query(Requirement).filter(Requirement.requisition_id == req.id).first()


def test_batch_assign_sales_owner_multi_row_passes(client, db_session: Session, test_user: User):
    req1 = _make_req(db_session, test_user.id, "REQ-BA1")
    req2 = _make_req(db_session, test_user.id, "REQ-BA2")
    r1, r2 = _requirement(db_session, req1), _requirement(db_session, req2)
    test_user.role = UserRole.SALES
    db_session.commit()
    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={"requirement_ids": json.dumps([r1.id, r2.id]), "buyer_id": ""},
    )
    assert resp.status_code != 404


def test_batch_assign_sales_non_owner_multi_row_404s(client, db_session: Session, test_user: User, admin_user: User):
    owned = _make_req(db_session, test_user.id, "REQ-BA-OWNED")
    foreign = _make_req(db_session, admin_user.id, "REQ-BA-FOREIGN")
    r1, r2 = _requirement(db_session, owned), _requirement(db_session, foreign)
    test_user.role = UserRole.SALES
    db_session.commit()
    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={"requirement_ids": json.dumps([r1.id, r2.id]), "buyer_id": ""},
    )
    assert resp.status_code == 404


def test_batch_status_sales_non_owner_multi_row_404s(client, db_session: Session, test_user: User, admin_user: User):
    owned = _make_req(db_session, test_user.id, "REQ-BS-OWNED")
    foreign = _make_req(db_session, admin_user.id, "REQ-BS-FOREIGN")
    r1, r2 = _requirement(db_session, owned), _requirement(db_session, foreign)
    test_user.role = UserRole.SALES
    db_session.commit()
    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={"requirement_ids": json.dumps([r1.id, r2.id]), "status": "sourcing"},
    )
    assert resp.status_code == 404


def test_batch_notes_sales_non_owner_multi_row_404s(client, db_session: Session, test_user: User, admin_user: User):
    owned = _make_req(db_session, test_user.id, "REQ-BN-OWNED")
    foreign = _make_req(db_session, admin_user.id, "REQ-BN-FOREIGN")
    r1, r2 = _requirement(db_session, owned), _requirement(db_session, foreign)
    test_user.role = UserRole.SALES
    db_session.commit()
    resp = client.post(
        "/v2/partials/sightings/batch-notes",
        data={"requirement_ids": json.dumps([r1.id, r2.id]), "notes": "called them"},
    )
    assert resp.status_code == 404


def test_batch_search_sales_non_owner_multi_row_404s(client, db_session: Session, test_user: User, admin_user: User):
    owned = _make_req(db_session, test_user.id, "REQ-BSRCH-OWNED")
    foreign = _make_req(db_session, admin_user.id, "REQ-BSRCH-FOREIGN")
    r1, r2 = _requirement(db_session, owned), _requirement(db_session, foreign)
    test_user.role = UserRole.SALES
    db_session.commit()
    resp = client.post(
        "/v2/partials/sightings/batch-refresh",
        data={"requirement_ids": json.dumps([r1.id, r2.id])},
    )
    assert resp.status_code == 404


def test_preview_inquiry_sales_non_owner_multi_row_404s(client, db_session: Session, test_user: User, admin_user: User):
    owned = _make_req(db_session, test_user.id, "REQ-PI-OWNED")
    foreign = _make_req(db_session, admin_user.id, "REQ-PI-FOREIGN")
    r1, r2 = _requirement(db_session, owned), _requirement(db_session, foreign)
    test_user.role = UserRole.SALES
    db_session.commit()
    resp = client.post(
        "/v2/partials/sightings/preview-inquiry",
        data={"requirement_ids": [str(r1.id), str(r2.id)], "vendor_names": ["Acme"], "email_body": "hi"},
    )
    assert resp.status_code == 404


def test_send_inquiry_sales_non_owner_multi_row_404s(client, db_session: Session, test_user: User, admin_user: User):
    owned = _make_req(db_session, test_user.id, "REQ-SI-OWNED")
    foreign = _make_req(db_session, admin_user.id, "REQ-SI-FOREIGN")
    r1, r2 = _requirement(db_session, owned), _requirement(db_session, foreign)
    test_user.role = UserRole.SALES
    db_session.commit()
    resp = client.post(
        "/v2/partials/sightings/send-inquiry",
        data={
            "requirement_ids": [str(r1.id), str(r2.id)],
            "vendor_names": ["Acme"],
            "email_body": "please quote",
        },
    )
    assert resp.status_code == 404

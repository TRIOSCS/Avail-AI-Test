"""Required-assignee guard on every task create surface.

Task 1 of the 2026-08-03 task-assignee-authz plan: the five manual
create_* functions in task_service raise ValueError("Assignee is
required") when assigned_to_id is None, and auto_create_task skips
(returns None, warn log) when its default-assignee chain resolves to
nobody. Task 2 extends this file with the requisition-board endpoint
422 backstop.

Called by: pytest
Depends on: conftest.py (db_session), app.services.task_service,
    app.models (Company, CustomerSite, SiteContact), app.models.auth,
    app.models.sourcing, app.models.vendors
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact
from app.models.auth import User
from app.models.sourcing import Requisition
from app.models.task import RequisitionTask
from app.models.vendors import VendorCard
from app.services import task_service

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_user(db: Session, role: str, email: str) -> User:
    u = User(
        email=email,
        name=email.split("@")[0],
        role=role,
        azure_id=f"az-{email}",
    )
    db.add(u)
    db.flush()
    return u


class _Scene:
    """One user plus one parent row of every task-scoping kind."""

    def __init__(self, db: Session):
        self.user = _make_user(db, "sales", "assignee-req@t.com")
        self.requisition = Requisition(name="Assignee Req", status="open", created_by=self.user.id)
        db.add(self.requisition)
        db.flush()
        self.company = Company(name="Assignee Co", is_active=True)
        db.add(self.company)
        db.flush()
        site = CustomerSite(company_id=self.company.id, site_name="HQ", is_active=True)
        db.add(site)
        db.flush()
        self.contact = SiteContact(customer_site_id=site.id, full_name="C One", is_active=True)
        db.add(self.contact)
        db.flush()
        self.vendor_card = VendorCard(
            normalized_name="assignee vendor",
            display_name="Assignee Vendor",
            emails=["v@t.com"],
            phones=[],
            sighting_count=0,
            created_at=datetime.now(UTC),
        )
        db.add(self.vendor_card)
        db.flush()

    def parent_kwargs(self, fn_name: str) -> dict:
        return {
            "create_task": {"requisition_id": self.requisition.id},
            "create_requisition_task": {"requisition_id": self.requisition.id},
            "create_company_task": {"company_id": self.company.id},
            "create_contact_task": {"site_contact_id": self.contact.id},
            "create_vendor_task": {"vendor_card_id": self.vendor_card.id},
        }[fn_name]


@pytest.fixture()
def scene(db_session: Session) -> _Scene:
    return _Scene(db_session)


# ---------------------------------------------------------------------------
# Manual creates: assigned_to_id=None must raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn_name",
    [
        "create_task",
        "create_requisition_task",
        "create_company_task",
        "create_contact_task",
        "create_vendor_task",
    ],
)
def test_manual_create_requires_assignee(db_session: Session, scene: _Scene, fn_name: str):
    fn = getattr(task_service, fn_name)
    with pytest.raises(ValueError, match="Assignee is required"):
        fn(
            db_session,
            title="T",
            assigned_to_id=None,
            created_by=scene.user.id,
            **scene.parent_kwargs(fn_name),
        )
    assert db_session.query(RequisitionTask).count() == 0


# ---------------------------------------------------------------------------
# Auto-create: unresolvable default assignee → skip (None), nothing persisted
# ---------------------------------------------------------------------------


def test_auto_create_skips_when_requisition_missing(db_session: Session):
    out = task_service.auto_create_task(
        db_session,
        requisition_id=999999,
        title="T",
        task_type="general",
        source_ref="offer:1",
    )
    assert out is None
    assert db_session.query(RequisitionTask).count() == 0


def test_auto_create_skips_when_requisition_has_no_owner(db_session: Session):
    req = Requisition(name="Ownerless", status="open", created_by=None, claimed_by_id=None)
    db_session.add(req)
    db_session.flush()
    out = task_service.auto_create_task(
        db_session,
        requisition_id=req.id,
        title="T",
        task_type="general",
        source_ref="offer:2",
    )
    assert out is None
    assert db_session.query(RequisitionTask).count() == 0

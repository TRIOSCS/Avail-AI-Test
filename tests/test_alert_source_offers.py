"""Tests for OfferConfirmedSource — the 'new confirmed offers' FYI alert.

Covers eligibility (approved + qualified + recent), the seen-drains-badge FYI
contract, qualification/status gating, the recency floor, and the ownership rule
(assigned_buyer_id, with the unassigned → requisition.created_by fallback).

Called by: pytest autodiscovery.
Depends on: conftest fixtures, app.services.alerts (record_seen),
            app.services.alerts.sources.offers.OfferConfirmedSource.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.constants import AlertKind, OfferStatus, QualificationStatus
from app.models.auth import User
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition
from app.services.alerts import record_seen
from app.services.alerts.sources.offers import OfferConfirmedSource


def _requirement_of(db: Session, req: Requisition) -> Requirement:
    return db.query(Requirement).filter(Requirement.requisition_id == req.id).first()


def _make_offer(
    db: Session,
    requirement: Requirement,
    *,
    status: str = OfferStatus.APPROVED,
    qualification_status: str | None = QualificationStatus.ESSENTIALS,
    approved_at: datetime | None = None,
) -> Offer:
    """Create a minimal valid offer tied to a requirement (and its requisition)."""
    if approved_at is None:
        approved_at = datetime.now(timezone.utc)
    offer = Offer(
        requisition_id=requirement.requisition_id,
        requirement_id=requirement.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        status=status,
        qualification_status=qualification_status,
        approved_at=approved_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return offer


@pytest.fixture()
def source() -> OfferConfirmedSource:
    return OfferConfirmedSource()


def test_source_identity(source: OfferConfirmedSource) -> None:
    assert source.key == "sales_hub_offers"
    assert source.kind == AlertKind.OFFER_CONFIRMED
    assert source.temperament.value == "fyi"


def test_approved_essentials_counts_and_appears(
    db_session: Session,
    test_user: User,
    test_requisition: Requisition,
    source: OfferConfirmedSource,
) -> None:
    """1. APPROVED + ESSENTIALS, approved_at now, on the user's requirement → 1."""
    requirement = _requirement_of(db_session, test_requisition)
    offer = _make_offer(db_session, requirement)

    assert source.count_for_user(db_session, test_user) == 1
    items = source.new_items_for_user(db_session, test_user)
    assert [i.ref_id for i in items] == [offer.id]
    assert items[0].anchor == f"req-{requirement.id}"


def test_seen_drains_the_badge(
    db_session: Session,
    test_user: User,
    test_requisition: Requisition,
    source: OfferConfirmedSource,
) -> None:
    """2. After record_seen, the FYI count drops to 0."""
    requirement = _requirement_of(db_session, test_requisition)
    offer = _make_offer(db_session, requirement)
    assert source.count_for_user(db_session, test_user) == 1

    record_seen(db_session, test_user, AlertKind.OFFER_CONFIRMED, offer.id)

    assert source.count_for_user(db_session, test_user) == 0
    assert source.new_items_for_user(db_session, test_user) == []


def test_unqualified_offer_not_counted(
    db_session: Session,
    test_user: User,
    test_requisition: Requisition,
    source: OfferConfirmedSource,
) -> None:
    """3. qualification_status unset, or non-approved status → not counted."""
    requirement = _requirement_of(db_session, test_requisition)

    # Approved but qualification not yet captured (NULL).
    _make_offer(db_session, requirement, qualification_status=None)
    # Qualified but not yet approved (pending review).
    _make_offer(
        db_session,
        requirement,
        status=OfferStatus.PENDING_REVIEW,
        qualification_status=QualificationStatus.COMPLETE,
    )

    assert source.count_for_user(db_session, test_user) == 0


def test_offer_older_than_recency_floor_not_counted(
    db_session: Session,
    test_user: User,
    test_requisition: Requisition,
    source: OfferConfirmedSource,
) -> None:
    """4. approved_at 60 days ago is below the (default 30-day) floor → not counted."""
    requirement = _requirement_of(db_session, test_requisition)
    old = datetime.now(timezone.utc) - timedelta(days=60)
    _make_offer(db_session, requirement, approved_at=old)

    assert source.count_for_user(db_session, test_user) == 0


def test_null_approved_at_not_counted(
    db_session: Session,
    test_user: User,
    test_requisition: Requisition,
    source: OfferConfirmedSource,
) -> None:
    """An APPROVED + qualified offer with NULL approved_at is not eligible."""
    requirement = _requirement_of(db_session, test_requisition)
    offer = _make_offer(db_session, requirement)
    offer.approved_at = None
    db_session.commit()

    assert source.count_for_user(db_session, test_user) == 0


def test_ownership_assigned_to_other_user(
    db_session: Session,
    test_user: User,
    sales_user: User,
    test_requisition: Requisition,
    source: OfferConfirmedSource,
) -> None:
    """5. Requirement assigned to a DIFFERENT user → counts for them, not test_user."""
    requirement = _requirement_of(db_session, test_requisition)
    requirement.assigned_buyer_id = sales_user.id
    db_session.commit()
    _make_offer(db_session, requirement)

    assert source.count_for_user(db_session, test_user) == 0
    assert source.count_for_user(db_session, sales_user) == 1


def test_ownership_unassigned_created_by_other_user(
    db_session: Session,
    test_user: User,
    sales_user: User,
    source: OfferConfirmedSource,
) -> None:
    """5b.

    Unassigned requirement on a requisition the OTHER user created → counts for the
    creator, not test_user.
    """
    req = Requisition(
        name="REQ-OTHER-001",
        status="active",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=10,
        assigned_buyer_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(requirement)
    db_session.commit()
    _make_offer(db_session, requirement)

    assert source.count_for_user(db_session, test_user) == 0
    assert source.count_for_user(db_session, sales_user) == 1


def test_ownership_unassigned_fallback_to_creator(
    db_session: Session,
    test_user: User,
    test_requisition: Requisition,
    source: OfferConfirmedSource,
) -> None:
    """6. assigned_buyer_id NULL but requisition.created_by == user → counted."""
    requirement = _requirement_of(db_session, test_requisition)
    # test_requisition.created_by == test_user.id; requirement is unassigned by default.
    assert requirement.assigned_buyer_id is None
    _make_offer(db_session, requirement)

    assert source.count_for_user(db_session, test_user) == 1

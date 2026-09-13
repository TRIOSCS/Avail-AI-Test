"""tests/test_vendor_coverage_service.py — Tests for
app/services/vendor_coverage_service.py.

Covers item 19: `coverage_ranked_vendor_rows` (formerly
`sightings._coverage_ranked_vendor_rows`) is importable and callable directly from its
new service module home, independent of the router alias that
tests/test_sightings_router.py already exercises in depth.

Called by: pytest
Depends on: app.services.vendor_coverage_service, app.models (Requisition, Requirement,
    VendorCard, VendorSightingSummary)
"""

from sqlalchemy.orm import Session

from app.models.sourcing import Requirement, Requisition
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard
from app.services.vendor_coverage_service import (
    RankedVendor,
    coverage_ranked_vendor_rows,
)


def _requirement(db: Session) -> Requirement:
    req = Requisition(name="Coverage Service RFQ", status="open", customer_name="CovSvc Co")
    db.add(req)
    db.flush()
    item = Requirement(requisition_id=req.id, primary_mpn="CS-MPN-1", target_qty=10, sourcing_status="open")
    db.add(item)
    db.flush()
    return item


def test_coverage_ranked_vendor_rows_importable_from_service_module(db_session: Session):
    """The function lives at its new home and returns RankedVendor rows for a cardless
    sighting, matching the pre-extraction contract."""
    item = _requirement(db_session)
    db_session.add(
        VendorSightingSummary(
            requirement_id=item.id,
            vendor_name="Direct Import Distributor",
            listing_count=1,
            score=50.0,
            vendor_card_id=None,
        )
    )
    db_session.commit()

    rows = coverage_ranked_vendor_rows(db_session, [item.id], set())

    assert len(rows) == 1
    assert isinstance(rows[0], RankedVendor)
    assert rows[0].card is None
    assert rows[0].vendor_name == "Direct Import Distributor"
    assert rows[0].has_contact is False


def test_coverage_ranked_vendor_rows_carded_vendor(db_session: Session):
    """A carded vendor's sighting groups by card.id and carries vendor_score through."""
    item = _requirement(db_session)
    card = VendorCard(
        normalized_name="direct import carded",
        display_name="Direct Import Carded",
        vendor_score=77.0,
    )
    db_session.add(card)
    db_session.flush()
    db_session.add(
        VendorSightingSummary(
            requirement_id=item.id,
            vendor_name=card.display_name,
            listing_count=1,
            score=60.0,
            vendor_card_id=card.id,
        )
    )
    db_session.commit()

    rows = coverage_ranked_vendor_rows(db_session, [item.id], set())

    assert len(rows) == 1
    assert rows[0].card is not None
    assert rows[0].card.id == card.id
    assert rows[0].vendor_score == 77.0


def test_sightings_alias_is_same_function(db_session: Session):
    """app.routers.sightings._coverage_ranked_vendor_rows is the identical function
    object — the router alias kept for the existing test suite (item 19)."""
    from app.routers.sightings import _coverage_ranked_vendor_rows

    assert _coverage_ranked_vendor_rows is coverage_ranked_vendor_rows

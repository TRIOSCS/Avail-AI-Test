"""Idea 3 — reply-likelihood vendor ranking.

Wire the already-persisted VendorCard.email_health_score into the RFQ-composer vendor
sort (a tiebreaker within a coverage tier) and thread it onto the ranked rows so the
modal can render a "reply health" chip. Coverage stays dominant.
"""

from app.models.sourcing import Requirement, Requisition
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard
from app.routers.sightings import _coverage_ranked_vendor_rows


def _seed(db, healths):
    req = Requisition(name="Rank RFQ", status="active", customer_name="X")
    db.add(req)
    db.flush()
    r = Requirement(requisition_id=req.id, primary_mpn="RANK-1", target_qty=10, sourcing_status="open")
    db.add(r)
    db.flush()
    for name, health in healths:
        card = VendorCard(normalized_name=name.lower(), display_name=name, email_health_score=health)
        db.add(card)
        db.flush()
        db.add(
            VendorSightingSummary(
                requirement_id=r.id,
                vendor_name=name,
                vendor_card_id=card.id,
                estimated_qty=10,
                listing_count=1,
                score=50.0,
            )
        )
    db.commit()
    return r


def test_higher_reply_health_ranks_first_within_coverage_tier(db_session):
    r = _seed(db_session, [("Vendor Low", 10.0), ("Vendor High", 90.0)])
    rows = _coverage_ranked_vendor_rows(db_session, [r.id], set())
    names = [row.vendor_name for row in rows]
    assert names.index("Vendor High") < names.index("Vendor Low")


def test_email_health_threaded_onto_ranked_row(db_session):
    r = _seed(db_session, [("Vendor High", 90.0)])
    rows = _coverage_ranked_vendor_rows(db_session, [r.id], set())
    by = {row.vendor_name: row for row in rows}
    assert by["Vendor High"].email_health_score == 90.0


def test_coverage_still_dominates_reply_health(db_session):
    # A vendor covering MORE parts outranks a higher-reply-health vendor covering fewer.
    req = Requisition(name="Cov RFQ", status="active", customer_name="X")
    db_session.add(req)
    db_session.flush()
    r1 = Requirement(requisition_id=req.id, primary_mpn="A", target_qty=1, sourcing_status="open")
    r2 = Requirement(requisition_id=req.id, primary_mpn="B", target_qty=1, sourcing_status="open")
    db_session.add_all([r1, r2])
    db_session.flush()
    broad = VendorCard(normalized_name="broad", display_name="Broad", email_health_score=5.0)
    narrow = VendorCard(normalized_name="narrow", display_name="Narrow", email_health_score=99.0)
    db_session.add_all([broad, narrow])
    db_session.flush()
    for rid in (r1.id, r2.id):
        db_session.add(
            VendorSightingSummary(
                requirement_id=rid,
                vendor_name="Broad",
                vendor_card_id=broad.id,
                estimated_qty=1,
                listing_count=1,
                score=50.0,
            )
        )
    db_session.add(
        VendorSightingSummary(
            requirement_id=r1.id,
            vendor_name="Narrow",
            vendor_card_id=narrow.id,
            estimated_qty=1,
            listing_count=1,
            score=50.0,
        )
    )
    db_session.commit()
    rows = _coverage_ranked_vendor_rows(db_session, [r1.id, r2.id], set())
    names = [row.vendor_name for row in rows]
    assert names.index("Broad") < names.index("Narrow")  # 2 parts beats higher reply health on 1

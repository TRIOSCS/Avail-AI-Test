"""test_data_health.py — Data Health Scanner.

Detects orphaned records, impossible status values, broken FK chains,
and stale computed fields. Creates realistic data scenarios and verifies
the system handles them correctly.

Called by: pytest tests/ux_mega/test_data_health.py
Depends on: conftest.py fixtures, app.models, app.services.integrity_service
"""

from datetime import datetime, timedelta, timezone

from app.models import (
    MaterialCard,
    Offer,
    Requirement,
    VendorCard,
)
from app.services.integrity_service import (
    check_dangling_fks,
    check_duplicate_cards,
    check_orphaned_offers,
    check_orphaned_requirements,
    heal_orphaned_records,
    run_integrity_check,
)


class TestOrphanedRecords:
    """Detect records with MPN but no material_card_id link."""

    def test_requirement_with_mpn_but_no_card(self, db_session, test_requisition):
        """Requirement has MPN but lost its material card link."""
        req = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="LM317T",
            normalized_mpn="lm317t",
            target_qty=100,
            material_card_id=None,  # orphaned!
        )
        db_session.add(req)
        db_session.flush()

        count = check_orphaned_requirements(db_session)
        assert count >= 1, "Should detect orphaned requirement"

    def test_offer_with_mpn_but_no_card(self, db_session, test_requisition):
        """Offer has MPN but lost its material card link."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.50,
            material_card_id=None,  # orphaned!
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.flush()

        count = check_orphaned_offers(db_session)
        assert count >= 1, "Should detect orphaned offer"

    def test_heal_relinks_orphaned_records(self, db_session, test_requisition):
        """heal_orphaned_records re-links records to material cards."""
        # Create a card
        card = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            manufacturer="TI",
        )
        db_session.add(card)
        db_session.flush()

        # Create orphaned requirement
        req = Requirement(
            requisition_id=test_requisition.id,
            primary_mpn="LM317T",
            normalized_mpn="lm317t",
            target_qty=100,
            material_card_id=None,
        )
        db_session.add(req)
        db_session.flush()

        result = heal_orphaned_records(db_session)
        assert result["requirements"] >= 1, "Should heal at least 1 requirement"


class TestStatusConsistency:
    """Detect impossible or inconsistent status combinations."""

    def test_offer_active_but_expired(self, db_session, test_requisition):
        """Offer marked active but expires_at is in the past = stale offer."""
        req = db_session.query(Requirement).filter_by(requisition_id=test_requisition.id).first()
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req.id if req else None,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.50,
            status="active",
            attribution_status="active",
            expires_at=datetime.now(timezone.utc) - timedelta(days=30),
            created_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        db_session.add(offer)
        db_session.flush()

        # Detect: active offers with expired dates
        stale = (
            db_session.query(Offer)
            .filter(
                Offer.status == "active",
                Offer.expires_at.isnot(None),
                Offer.expires_at < datetime.now(timezone.utc),
            )
            .count()
        )
        assert stale >= 1, "Should detect stale active offer with past expiry"

    def test_requirement_status_valid_values(self, db_session, test_requisition):
        """All requirements have valid sourcing_status values."""
        valid_statuses = {"open", "sourcing", "offered", "quoted", "won", "lost"}
        reqs = (
            db_session.query(Requirement.sourcing_status)
            .filter(Requirement.requisition_id == test_requisition.id)
            .all()
        )
        for (status,) in reqs:
            assert status in valid_statuses, f"Invalid sourcing_status: {status}"


class TestFKChainIntegrity:
    """Verify the Requisition → Requirement → Offer → Quote → BuyPlan chain."""

    def test_quote_lines_reference_valid_offers(self, db_session, test_quote):
        """All quote line offer_ids point to existing offers."""

        # QuoteLines with offer_id should reference existing offers
        from app.models import QuoteLine

        lines = (
            db_session.query(QuoteLine)
            .filter(
                QuoteLine.quote_id == test_quote.id,
                QuoteLine.offer_id.isnot(None),
            )
            .all()
        )
        for line in lines:
            offer = db_session.get(Offer, line.offer_id)
            assert offer is not None, f"QuoteLine {line.id} references missing offer {line.offer_id}"

    def test_dangling_material_card_fks(self, db_session):
        """No records point to non-existent material cards."""
        dangling = check_dangling_fks(db_session)
        total = sum(dangling.values())
        assert total == 0, f"Found {total} dangling material card FKs: {dangling}"


class TestDuplicateDetection:
    """Detect duplicate records that shouldn't exist."""

    def test_no_duplicate_material_cards(self, db_session):
        """Each normalized_mpn should appear at most once."""
        dupes = check_duplicate_cards(db_session)
        assert dupes == 0, f"Found {dupes} duplicate material card MPNs"

    def test_no_duplicate_vendor_cards(self, db_session):
        """Each normalized vendor name should appear at most once."""
        from sqlalchemy import func

        dupes = (
            db_session.query(VendorCard.normalized_name, func.count(VendorCard.id))
            .group_by(VendorCard.normalized_name)
            .having(func.count(VendorCard.id) > 1)
            .all()
        )
        assert len(dupes) == 0, f"Found duplicate vendor cards: {dupes}"


class TestIntegrityServiceIntegration:
    """Verify the full integrity check + heal pipeline."""

    def test_full_integrity_check_runs(self, db_session):
        """run_integrity_check completes without error and returns report."""
        report = run_integrity_check(db_session)
        assert "status" in report
        assert report["status"] in ("healthy", "degraded", "critical")
        assert "checks" in report
        assert "healed" in report

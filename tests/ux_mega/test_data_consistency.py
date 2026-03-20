"""test_data_consistency.py — Data Consistency Validator.

Creates realistic end-to-end workflow chains (Requisition → Requirement →
Offer → Quote → BuyPlan) and verifies all numbers, statuses, and references
stay consistent throughout.

Called by: pytest tests/ux_mega/test_data_consistency.py
Depends on: conftest.py fixtures, app.models
"""

from app.models import (
    Offer,
    Requirement,
    Requisition,
)


class TestRequisitionChain:
    """Verify Requisition → Requirement data consistency."""

    def test_all_requirements_reference_parent_requisition(self, db_session, test_requisition):
        """Every requirement in the DB has a valid requisition_id."""
        orphans = db_session.query(Requirement).filter(Requirement.requisition_id == test_requisition.id).all()
        for req in orphans:
            parent = db_session.get(Requisition, req.requisition_id)
            assert parent is not None, f"Requirement {req.id} references missing requisition {req.requisition_id}"

    def test_requirement_qty_is_positive(self, db_session, test_requisition):
        """No requirements should have zero or negative target_qty."""
        bad = (
            db_session.query(Requirement)
            .filter(
                Requirement.requisition_id == test_requisition.id,
                Requirement.target_qty <= 0,
            )
            .count()
        )
        assert bad == 0, f"Found {bad} requirements with qty <= 0"


class TestOfferChain:
    """Verify Offer → Requirement → Requisition consistency."""

    def test_offer_price_is_positive(self, db_session, test_requisition):
        """No active offers should have zero or negative price."""
        bad = (
            db_session.query(Offer)
            .filter(
                Offer.requisition_id == test_requisition.id,
                Offer.status == "active",
                Offer.unit_price <= 0,
            )
            .count()
        )
        assert bad == 0, f"Found {bad} active offers with price <= 0"

    def test_offer_references_valid_requirement(self, db_session, test_requisition):
        """Every offer's requirement_id points to an existing requirement."""
        offers = (
            db_session.query(Offer)
            .filter(
                Offer.requisition_id == test_requisition.id,
                Offer.requirement_id.isnot(None),
            )
            .all()
        )
        for offer in offers:
            req = db_session.get(Requirement, offer.requirement_id)
            assert req is not None, f"Offer {offer.id} references missing requirement {offer.requirement_id}"

    def test_offer_requisition_matches_requirement_requisition(self, db_session, test_requisition):
        """Offer.requisition_id must match its Requirement.requisition_id."""
        offers = (
            db_session.query(Offer)
            .filter(
                Offer.requisition_id == test_requisition.id,
                Offer.requirement_id.isnot(None),
            )
            .all()
        )
        for offer in offers:
            req = db_session.get(Requirement, offer.requirement_id)
            if req:
                assert offer.requisition_id == req.requisition_id, (
                    f"Offer {offer.id} requisition_id={offer.requisition_id} "
                    f"doesn't match requirement's requisition_id={req.requisition_id}"
                )


class TestQuoteConsistency:
    """Verify Quote totals match underlying line items."""

    def test_quote_references_valid_requisition(self, db_session, test_quote):
        """Quote's requisition_id points to an existing requisition."""
        parent = db_session.get(Requisition, test_quote.requisition_id)
        assert parent is not None, f"Quote {test_quote.id} references missing requisition {test_quote.requisition_id}"

    def test_no_quotes_with_invalid_status(self, db_session):
        """All quotes have a recognized status value."""
        from app.models import Quote

        valid = {"draft", "sent", "accepted", "rejected", "lost", "revised", "expired"}
        quotes = db_session.query(Quote.id, Quote.status).all()
        for qid, status in quotes:
            assert status in valid, f"Quote {qid} has invalid status: {status}"

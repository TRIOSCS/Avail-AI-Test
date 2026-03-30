"""Tests for CRM Phase 3 — vendor discovery and MPN search.

Called by: pytest
Depends on: app.models.vendors, app.models.sourcing, app.models.intelligence
"""

from sqlalchemy.orm import Session

from app.models.vendors import VendorCard
from tests.conftest import engine  # noqa: F401


class TestJsonbTagColumns:
    """Test that VendorCard tag columns accept JSONB data."""

    def test_brand_tags_accepts_list(self, db_session: Session):
        """VendorCard.brand_tags stores a list."""
        v = VendorCard(
            normalized_name="test vendor",
            display_name="Test Vendor",
            brand_tags=["TI", "NXP", "ST"],
        )
        db_session.add(v)
        db_session.flush()
        assert v.brand_tags == ["TI", "NXP", "ST"]

    def test_commodity_tags_accepts_list(self, db_session: Session):
        """VendorCard.commodity_tags stores a list."""
        v = VendorCard(
            normalized_name="test vendor 2",
            display_name="Test Vendor 2",
            commodity_tags=["Microcontrollers", "Memory"],
        )
        db_session.add(v)
        db_session.flush()
        assert v.commodity_tags == ["Microcontrollers", "Memory"]

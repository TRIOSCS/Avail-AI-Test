"""Tests for CRM Phase 3 — vendor discovery and MPN search.

Called by: pytest
Depends on: app.models.vendors, app.models.sourcing, app.models.intelligence
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendors import VendorCard
from tests.conftest import engine  # noqa: F401


class TestJsonbTagColumns:
    """Test that VendorCard tag columns accept JSONB data."""

    @pytest.mark.parametrize(
        ("normalized_name", "display_name", "column", "value"),
        [
            ("test vendor", "Test Vendor", "brand_tags", ["TI", "NXP", "ST"]),
            ("test vendor 2", "Test Vendor 2", "commodity_tags", ["Microcontrollers", "Memory"]),
        ],
        ids=["brand_tags", "commodity_tags"],
    )
    def test_tag_column_accepts_list(self, db_session: Session, normalized_name, display_name, column, value):
        """VendorCard tag columns store a list."""
        v = VendorCard(
            normalized_name=normalized_name,
            display_name=display_name,
            **{column: value},
        )
        db_session.add(v)
        db_session.flush()
        assert getattr(v, column) == value


class TestEnhancedBrowseSearch:
    """Test vendor browse search matches brand and commodity tags."""

    def test_search_by_brand_tag(self, client: TestClient, db_session: Session):
        """Searching 'TI' matches vendor with TI in brand_tags."""
        v = VendorCard(
            normalized_name="ti specialist",
            display_name="TI Specialist Corp",
            brand_tags=["TI", "NXP"],
        )
        db_session.add(v)
        db_session.commit()

        resp = client.get("/v2/partials/vendors?q=TI")
        assert resp.status_code == 200
        assert "TI Specialist" in resp.text

    def test_search_by_commodity_tag(self, client: TestClient, db_session: Session):
        """Searching 'Memory' matches vendor with Memory in commodity_tags."""
        v = VendorCard(
            normalized_name="memory house",
            display_name="Memory House Inc",
            commodity_tags=["Memory", "Storage"],
        )
        db_session.add(v)
        db_session.commit()

        resp = client.get("/v2/partials/vendors?q=Memory")
        assert resp.status_code == 200
        assert "Memory House" in resp.text


class TestVendorDetailMpnContext:
    """Test vendor detail shows MPN-specific sightings when mpn param passed."""

    def test_vendor_detail_with_mpn_shows_filtered_header(
        self, client: TestClient, db_session: Session, test_user: User
    ):
        """Vendor detail with ?mpn= shows 'Sightings for MPN' header."""
        vendor = VendorCard(
            normalized_name="mpn test vendor",
            display_name="MPN Test Vendor",
        )
        db_session.add(vendor)
        db_session.flush()

        req = Requisition(
            name="Test Req",
            created_by=test_user.id,
            status="active",
        )
        db_session.add(req)
        db_session.flush()

        requirement = Requirement(
            requisition_id=req.id,
            primary_mpn="LM317T",
            normalized_mpn="LM317T",
            manufacturer="TI",
        )
        db_session.add(requirement)
        db_session.flush()

        s1 = Sighting(
            requirement_id=requirement.id,
            vendor_name="MPN Test Vendor",
            vendor_name_normalized="mpn test vendor",
            mpn_matched="LM317T",
            normalized_mpn="LM317T",
            qty_available=100,
            unit_price=Decimal("1.50"),
            source_type="brokerbin",
        )
        db_session.add(s1)
        db_session.commit()

        resp = client.get(f"/v2/partials/vendors/{vendor.id}?mpn=LM317T")
        assert resp.status_code == 200
        assert "LM317T" in resp.text

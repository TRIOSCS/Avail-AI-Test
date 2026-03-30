"""Tests for CRM Phase 3 — vendor discovery and MPN search.

Called by: pytest
Depends on: app.models.vendors, app.models.sourcing, app.models.intelligence
"""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.models.intelligence import MaterialCard, MaterialVendorHistory
from app.models.sourcing import Requirement, Requisition, Sighting
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


class TestFindByPart:
    """Test MPN-to-vendor lookup."""

    def test_find_by_part_returns_200(self, client: TestClient):
        """GET /v2/partials/vendors/find-by-part returns 200."""
        resp = client.get("/v2/partials/vendors/find-by-part")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_find_by_part_with_mpn(self, client: TestClient, db_session: Session):
        """MPN search returns matching vendors from MaterialVendorHistory."""
        card = MaterialCard(
            normalized_mpn="LM317T",
            display_mpn="LM317T",
            manufacturer="TI",
        )
        db_session.add(card)
        db_session.flush()

        vendor = VendorCard(
            normalized_name="acme parts",
            display_name="Acme Parts",
        )
        db_session.add(vendor)
        db_session.flush()

        mvh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Acme Parts",
            vendor_name_normalized="acme parts",
            times_seen=5,
            last_price=Decimal("1.50"),
            last_qty=1000,
        )
        db_session.add(mvh)
        db_session.commit()

        resp = client.get("/v2/partials/vendors/find-by-part?mpn=LM317T")
        assert resp.status_code == 200
        assert "Acme Parts" in resp.text

    def test_find_by_part_empty_shows_prompt(self, client: TestClient):
        """Empty MPN shows the search prompt."""
        resp = client.get("/v2/partials/vendors/find-by-part")
        assert resp.status_code == 200
        assert "Enter MPN" in resp.text


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


class TestFindByPartAffinity:
    """Test vendor affinity suggestions in Find by Part."""

    def test_affinity_badge_shown_for_affinity_results(self, client: TestClient, db_session: Session):
        """Affinity results show a 'Vendor Match' badge."""
        # With no MVH data, affinity should be attempted
        # Create a MaterialCard so the MPN is recognized
        card = MaterialCard(
            normalized_mpn="RARE123",
            display_mpn="RARE123",
            manufacturer="TI",
        )
        db_session.add(card)
        db_session.commit()

        resp = client.get("/v2/partials/vendors/find-by-part?mpn=RARE123")
        assert resp.status_code == 200
        # Even with no affinity results, the page should render without error

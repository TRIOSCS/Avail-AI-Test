"""
tests/test_customer_analysis_service.py — Tests for customer material analysis service.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, Requirement, Requisition, Sighting


@pytest.fixture()
def company_with_reqs(db_session: Session):
    """Company with sites, requisitions, and requirements for tag analysis."""
    co = Company(
        name="TagCo Inc",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.flush()

    site = CustomerSite(
        company_id=co.id,
        site_name="TagCo HQ",
        is_active=True,
    )
    db_session.add(site)
    db_session.flush()

    req = Requisition(
        name="TAG-REQ-001",
        customer_site_id=site.id,
        status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    # Add multiple requirements with brand info
    for mpn, brand in [
        ("7945-AC1", "IBM"),
        ("DL380-G10", "HP"),
        ("PowerEdge-R740", "Dell"),
        ("7042-CR8", "IBM"),
        ("WS-C3850-24T", "Cisco"),
    ]:
        db_session.add(
            Requirement(
                requisition_id=req.id,
                primary_mpn=mpn,
                brand=brand,
                created_at=datetime.now(timezone.utc),
            )
        )

    # Add sightings as well
    for mpn, mfr in [
        ("EX4300-48T", "Juniper"),
        ("N9K-C93180YC-FX", "Cisco"),
    ]:
        # Need a requirement to link sightings to
        r = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(r)
        db_session.flush()
        db_session.add(
            Sighting(
                requirement_id=r.id,
                vendor_name="test_vendor",
                mpn_matched=mpn,
                manufacturer=mfr,
                created_at=datetime.now(timezone.utc),
            )
        )

    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def company_no_reqs(db_session: Session):
    """Company with no sites/requisitions."""
    co = Company(
        name="EmptyCo",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


class TestAnalyzeCustomerMaterials:
    @patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
    )
    @pytest.mark.asyncio
    async def test_analyze_with_data(self, mock_claude, db_session, company_with_reqs):
        """Analysis with requirements generates brand and commodity tags."""
        mock_claude.return_value = {
            "brands": ["IBM", "Cisco"],
            "commodities": ["Server", "Networking"],
        }
        from app.services.customer_analysis_service import analyze_customer_materials

        await analyze_customer_materials(company_with_reqs.id, db_session=db_session)

        db_session.refresh(company_with_reqs)
        assert company_with_reqs.brand_tags == ["IBM", "Cisco"]
        assert company_with_reqs.commodity_tags == ["Server", "Networking"]
        assert company_with_reqs.material_tags_updated_at is not None
        mock_claude.assert_called_once()

    @patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
    )
    @pytest.mark.asyncio
    async def test_analyze_no_requisitions(self, mock_claude, db_session, company_no_reqs):
        """Analysis with no sites returns early without calling Claude."""
        from app.services.customer_analysis_service import analyze_customer_materials

        await analyze_customer_materials(company_no_reqs.id, db_session=db_session)
        mock_claude.assert_not_called()
        db_session.refresh(company_no_reqs)
        assert company_no_reqs.brand_tags is None or company_no_reqs.brand_tags == []

    @patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
    )
    @pytest.mark.asyncio
    async def test_analyze_with_site_but_no_parts(self, mock_claude, db_session):
        """Company has site but no requisitions → no Claude call."""
        co = Company(name="SiteOnly", is_active=True, created_at=datetime.now(timezone.utc))
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="Empty Site", is_active=True)
        db_session.add(site)
        db_session.commit()

        from app.services.customer_analysis_service import analyze_customer_materials

        await analyze_customer_materials(co.id, db_session=db_session)
        mock_claude.assert_not_called()

    @patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
    )
    @pytest.mark.asyncio
    async def test_analyze_invalid_company(self, mock_claude, db_session):
        """Analysis for non-existent company returns early."""
        from app.services.customer_analysis_service import analyze_customer_materials

        await analyze_customer_materials(999999, db_session=db_session)
        mock_claude.assert_not_called()

    @patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
    )
    @pytest.mark.asyncio
    async def test_analyze_claude_returns_none(self, mock_claude, db_session, company_with_reqs):
        """If Claude returns None, tags are not updated."""
        mock_claude.return_value = None
        from app.services.customer_analysis_service import analyze_customer_materials

        await analyze_customer_materials(company_with_reqs.id, db_session=db_session)
        db_session.refresh(company_with_reqs)
        # Tags should remain unset
        assert company_with_reqs.material_tags_updated_at is None

    @patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
    )
    @pytest.mark.asyncio
    async def test_analyze_claude_returns_empty(self, mock_claude, db_session, company_with_reqs):
        """If Claude returns empty arrays, tags are set to empty."""
        mock_claude.return_value = {"brands": [], "commodities": []}
        from app.services.customer_analysis_service import analyze_customer_materials

        await analyze_customer_materials(company_with_reqs.id, db_session=db_session)
        db_session.refresh(company_with_reqs)
        assert company_with_reqs.brand_tags == []
        assert company_with_reqs.commodity_tags == []
        assert company_with_reqs.material_tags_updated_at is not None

    @patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
    )
    @pytest.mark.asyncio
    async def test_analyze_truncates_to_five(self, mock_claude, db_session, company_with_reqs):
        """Tags are truncated to max 5 entries."""
        mock_claude.return_value = {
            "brands": ["A", "B", "C", "D", "E", "F", "G"],
            "commodities": ["X", "Y", "Z", "W", "V", "U"],
        }
        from app.services.customer_analysis_service import analyze_customer_materials

        await analyze_customer_materials(company_with_reqs.id, db_session=db_session)
        db_session.refresh(company_with_reqs)
        assert len(company_with_reqs.brand_tags) == 5
        assert len(company_with_reqs.commodity_tags) == 5

    @patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
    )
    @patch("app.database.SessionLocal")
    @pytest.mark.asyncio
    async def test_analyze_own_session_exception(self, mock_session_local, mock_claude, db_session, company_with_reqs):
        """Exception with own_session=True triggers rollback + close."""
        mock_db = mock_session_local.return_value
        mock_db.get.side_effect = Exception("DB error")
        from app.services.customer_analysis_service import analyze_customer_materials

        # Should not raise, exception is caught internally
        await analyze_customer_materials(company_with_reqs.id, db_session=None)
        mock_db.rollback.assert_called_once()
        mock_db.close.assert_called_once()

    @patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
    )
    @patch("app.database.SessionLocal")
    @pytest.mark.asyncio
    async def test_analyze_own_session_close(self, mock_session_local, mock_claude):
        """Own session is always closed in finally block."""
        mock_db = mock_session_local.return_value
        # Return None from db.get to trigger early return
        mock_db.get.return_value = None
        from app.services.customer_analysis_service import analyze_customer_materials

        await analyze_customer_materials(999, db_session=None)
        mock_db.close.assert_called_once()
        mock_claude.assert_not_called()

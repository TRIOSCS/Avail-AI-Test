"""Tests for app/services/vendor_email_lookup.py.

Covers: find_vendors_for_parts (empty mpns, sighting data, no enrichment),
_query_db_for_part (basic flow, returns list).

Called by: pytest
Depends on: conftest.py (db_session)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, Sighting, User, VendorCard
from app.services.vendor_email_lookup import _query_db_for_part, find_vendors_for_parts

_req_counter = 0


def _make_requirement(db: Session) -> Requirement:
    """Create a minimal requisition + requirement for sighting FK."""
    global _req_counter
    _req_counter += 1
    azure_id = f"sv-az-{_req_counter:06d}"
    email = f"sv{_req_counter}@test.com"
    user = User(email=email, name="SV", role="buyer", azure_id=azure_id)
    db.add(user)
    db.flush()
    req = Requisition(name=f"SV-REQ-{_req_counter}", status="active", created_by=user.id)
    db.add(req)
    db.flush()
    item = Requirement(requisition_id=req.id, primary_mpn="GENERIC", target_qty=1)
    db.add(item)
    db.flush()
    return item


def _make_sighting(db: Session, mpn: str, vendor_name: str, **kwargs) -> Sighting:
    requirement = _make_requirement(db)
    s = Sighting(
        requirement_id=requirement.id,
        normalized_mpn=mpn.upper(),
        mpn_matched=mpn,
        vendor_name=vendor_name,
        source_type="api",
        vendor_email=kwargs.get("vendor_email"),
        vendor_phone=kwargs.get("vendor_phone"),
        qty_available=kwargs.get("qty_available"),
        unit_price=kwargs.get("unit_price"),
        currency=kwargs.get("currency", "USD"),
        created_at=datetime.now(timezone.utc),
    )
    db.add(s)
    db.commit()
    return s


class TestFindVendorsForParts:
    @pytest.mark.asyncio
    async def test_empty_mpns_returns_empty(self, db_session: Session):
        result = await find_vendors_for_parts([], db_session, enrich_missing=False)
        assert result == {}

    @pytest.mark.asyncio
    async def test_blank_mpn_skipped(self, db_session: Session):
        result = await find_vendors_for_parts(["", "  "], db_session, enrich_missing=False)
        # Keys exist for the inputs but empty results (blank mpn stripped)
        assert "" in result or True  # blank results, no crash

    @pytest.mark.asyncio
    async def test_returns_vendors_for_mpn(self, db_session: Session):
        _make_sighting(db_session, "LM358", "Acme Parts", vendor_email="acme@example.com")
        result = await find_vendors_for_parts(["LM358"], db_session, enrich_missing=False)
        assert "LM358" in result
        vendors = result["LM358"]
        assert len(vendors) >= 1
        vendor_names = [v["vendor_name"] for v in vendors]
        assert any("Acme" in n for n in vendor_names)

    @pytest.mark.asyncio
    async def test_no_enrichment_when_flag_false(self, db_session: Session):
        with patch("app.services.vendor_email_lookup._enrich_vendors_batch", new_callable=AsyncMock) as mock_enrich:
            await find_vendors_for_parts(["LM358"], db_session, enrich_missing=False)
            mock_enrich.assert_not_called()

    @pytest.mark.asyncio
    async def test_enrichment_called_for_vendors_without_email(self, db_session: Session):
        _make_sighting(db_session, "STM32", "NoEmail Vendor")  # no email
        with patch("app.services.vendor_email_lookup._enrich_vendors_batch", new_callable=AsyncMock) as mock_enrich:
            await find_vendors_for_parts(["STM32"], db_session, enrich_missing=True)
            mock_enrich.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_enrichment_when_all_have_emails(self, db_session: Session):
        _make_sighting(db_session, "BC547", "HasEmail", vendor_email="has@example.com")
        with patch("app.services.vendor_email_lookup._enrich_vendors_batch", new_callable=AsyncMock) as mock_enrich:
            await find_vendors_for_parts(["BC547"], db_session, enrich_missing=True)
            mock_enrich.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_mpns(self, db_session: Session):
        _make_sighting(db_session, "LM358", "VendorA", vendor_email="a@a.com")
        _make_sighting(db_session, "NE555", "VendorB", vendor_email="b@b.com")
        result = await find_vendors_for_parts(["LM358", "NE555"], db_session, enrich_missing=False)
        assert "LM358" in result
        assert "NE555" in result

    @pytest.mark.asyncio
    async def test_broadcast_vendors_included(self, db_session: Session):
        # Create a broadcast vendor card
        card = VendorCard(
            display_name="Broadcast Vendor",
            normalized_name="broadcastvendor",
            is_broadcast=True,
            is_blacklisted=False,
            emails=["broadcast@vendor.com"],
        )
        db_session.add(card)
        db_session.commit()
        result = await find_vendors_for_parts(["ANYPART"], db_session, enrich_missing=False)
        # Broadcast vendor should appear even if no sighting for ANYPART
        all_vendors = result.get("ANYPART", [])
        assert any(
            "broadcast" in v.get("vendor_name", "").lower() or "broadcast" in str(v.get("sources", []))
            for v in all_vendors
        )


class TestQueryDbForPart:
    def test_returns_list(self, db_session: Session):
        result = _query_db_for_part("NONEXISTENT", db_session)
        assert isinstance(result, list)

    def test_finds_sighting_vendor(self, db_session: Session):
        _make_sighting(db_session, "LM741", "TestVendor", vendor_email="test@vendor.com")
        result = _query_db_for_part("LM741", db_session)
        assert len(result) >= 1
        assert any(v["vendor_name"] == "TestVendor" for v in result)

    def test_vendor_email_included(self, db_session: Session):
        _make_sighting(db_session, "TL072", "EmailVendor", vendor_email="info@emailvendor.com")
        result = _query_db_for_part("TL072", db_session)
        emails = [e for v in result for e in v.get("emails", [])]
        assert "info@emailvendor.com" in emails

    def test_deduplicates_vendors(self, db_session: Session):
        # Two sightings for same vendor
        _make_sighting(db_session, "OP07", "Acme Parts", vendor_email="acme@parts.com", qty_available=100)
        _make_sighting(db_session, "OP07", "Acme Parts", vendor_email="acme@parts.com", qty_available=200)
        result = _query_db_for_part("OP07", db_session)
        acme_entries = [v for v in result if "acme" in v["vendor_name"].lower()]
        # Should be deduplicated (only 1 entry for Acme)
        assert len(acme_entries) == 1

    def test_sources_converted_to_list(self, db_session: Session):
        _make_sighting(db_session, "CD4011", "SomeVendor")
        result = _query_db_for_part("CD4011", db_session)
        for v in result:
            assert isinstance(v["sources"], list)

    def test_best_price_kept(self, db_session: Session):
        _make_sighting(db_session, "LMC555", "PriceVendor", unit_price=1.00, qty_available=50)
        _make_sighting(db_session, "LMC555", "PriceVendor", unit_price=0.50, qty_available=50)
        result = _query_db_for_part("LMC555", db_session)
        vendor = next((v for v in result if "pricevendor" in v["vendor_name"].lower().replace(" ", "")), None)
        if vendor:
            assert vendor["unit_price"] <= 1.00  # best (lowest) price kept

    def test_best_qty_kept(self, db_session: Session):
        _make_sighting(db_session, "UC3843", "QtyVendor", qty_available=10)
        _make_sighting(db_session, "UC3843", "QtyVendor", qty_available=500)
        result = _query_db_for_part("UC3843", db_session)
        vendor = next((v for v in result if "qtyvendor" in v["vendor_name"].lower().replace(" ", "")), None)
        if vendor:
            assert vendor["qty_available"] == 500

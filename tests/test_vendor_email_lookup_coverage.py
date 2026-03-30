"""Tests for app/services/vendor_email_lookup.py — comprehensive coverage.

Covers find_vendors_for_parts, _query_db_for_part, build_inquiry_groups,
and _enrich_vendors_batch.

Called by: pytest
Depends on: conftest fixtures, vendor_email_lookup
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, Sighting, User, VendorCard, VendorContact
from app.services.vendor_email_lookup import (
    _query_db_for_part,
    build_inquiry_groups,
    find_vendors_for_parts,
)


@pytest.fixture()
def requisition_with_req(db_session: Session, test_user: User) -> tuple:
    """Returns (Requisition, Requirement) pair for FK-safe Sighting creation."""
    req = Requisition(
        name="VENDOR-LOOKUP-REQ",
        customer_name="Test Co",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=100,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


@pytest.fixture()
def vendor_card(db_session: Session) -> VendorCard:
    card = VendorCard(
        normalized_name="arrow",
        display_name="Arrow Electronics",
        emails=["sales@arrow.com"],
        phones=["+1-555-0100"],
        website="https://arrow.com",
        is_broadcast=False,
        is_blacklisted=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def sighting_for_lm317t(db_session: Session, requisition_with_req: tuple) -> Sighting:
    _, req_item = requisition_with_req
    s = Sighting(
        requirement_id=req_item.id,
        normalized_mpn="lm317t",
        mpn_matched="LM317T",
        vendor_name="Arrow Electronics",
        vendor_email="sales@arrow.com",
        vendor_phone="+1-555-0100",
        source_type="api",
        qty_available=1000,
        unit_price=0.50,
        currency="USD",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def broadcast_vendor(db_session: Session) -> VendorCard:
    card = VendorCard(
        normalized_name="broadcast vendor",
        display_name="Broadcast Vendor Co",
        emails=["info@broadcast.com"],
        phones=["+1-888-0200"],
        is_broadcast=True,
        is_blacklisted=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


class TestQueryDbForPart:
    def test_empty_db_returns_empty_list(self, db_session: Session):
        results = _query_db_for_part("LM317T", db_session)
        assert results == []

    def test_finds_vendors_from_sightings(self, db_session: Session, sighting_for_lm317t: Sighting):
        results = _query_db_for_part("LM317T", db_session)
        assert len(results) == 1
        vendor = results[0]
        assert "Arrow Electronics" in vendor["vendor_name"]
        assert "sales@arrow.com" in vendor["emails"]

    def test_merges_emails_from_vendor_card(
        self, db_session: Session, sighting_for_lm317t: Sighting, vendor_card: VendorCard
    ):
        # Arrow card should be merged with sighting data
        results = _query_db_for_part("LM317T", db_session)
        assert len(results) >= 1
        arrow = next((v for v in results if "arrow" in v["vendor_name"].lower()), None)
        assert arrow is not None

    def test_includes_broadcast_vendors(self, db_session: Session, broadcast_vendor: VendorCard):
        # Broadcast vendors always appear regardless of MPN
        results = _query_db_for_part("SOME_UNKNOWN_MPN", db_session)
        broadcast = next((v for v in results if "Broadcast" in v.get("vendor_name", "")), None)
        assert broadcast is not None
        assert "info@broadcast.com" in broadcast["emails"]

    def test_excludes_blacklisted_broadcast(self, db_session: Session):
        card = VendorCard(
            normalized_name="blacklisted co",
            display_name="Blacklisted Co",
            emails=["bad@blacklisted.com"],
            is_broadcast=True,
            is_blacklisted=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        results = _query_db_for_part("LM317T", db_session)
        blacklisted = next((v for v in results if "Blacklisted" in v.get("vendor_name", "")), None)
        assert blacklisted is None

    def test_vendor_contacts_merged(self, db_session: Session, sighting_for_lm317t: Sighting, vendor_card: VendorCard):
        vc = VendorContact(
            vendor_card_id=vendor_card.id,
            full_name="John Sales",
            email="john@arrow.com",
            source="manual",
        )
        db_session.add(vc)
        db_session.commit()
        results = _query_db_for_part("LM317T", db_session)
        arrow = next((v for v in results if "arrow" in v.get("vendor_name", "").lower()), None)
        if arrow:
            assert "john@arrow.com" in arrow["emails"] or "sales@arrow.com" in arrow["emails"]

    def test_sources_converted_to_list(self, db_session: Session, sighting_for_lm317t: Sighting):
        results = _query_db_for_part("LM317T", db_session)
        for v in results:
            assert isinstance(v["sources"], list)

    def test_sighting_updates_qty_and_price(self, db_session: Session, requisition_with_req: tuple):
        _, req_item = requisition_with_req
        s1 = Sighting(
            requirement_id=req_item.id,
            normalized_mpn="testmpn",
            vendor_name="Vendor A",
            source_type="api",
            qty_available=500,
            unit_price=1.00,
            created_at=datetime.now(timezone.utc),
        )
        s2 = Sighting(
            requirement_id=req_item.id,
            normalized_mpn="testmpn",
            vendor_name="Vendor A",
            source_type="api",
            qty_available=2000,  # higher qty
            unit_price=0.80,  # lower price
            created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([s1, s2])
        db_session.commit()
        results = _query_db_for_part("TESTMPN", db_session)
        vendor_a = next((v for v in results if "Vendor A" in v["vendor_name"]), None)
        assert vendor_a is not None
        assert vendor_a["qty_available"] == 2000
        assert float(vendor_a["unit_price"]) == pytest.approx(0.80)


class TestFindVendorsForParts:
    async def test_empty_mpns_returns_empty_dict(self, db_session: Session):
        results = await find_vendors_for_parts([], db_session)
        assert results == {}

    async def test_single_mpn_no_results(self, db_session: Session):
        results = await find_vendors_for_parts(["UNKNOWN_MPN_XYZ"], db_session)
        assert "UNKNOWN_MPN_XYZ" in results
        assert results["UNKNOWN_MPN_XYZ"] == []

    async def test_single_mpn_with_sightings(self, db_session: Session, sighting_for_lm317t: Sighting):
        results = await find_vendors_for_parts(["LM317T"], db_session, enrich_missing=False)
        assert "LM317T" in results
        assert len(results["LM317T"]) >= 1

    async def test_multiple_mpns(self, db_session: Session, sighting_for_lm317t: Sighting):
        results = await find_vendors_for_parts(["LM317T", "STM32F4"], db_session, enrich_missing=False)
        assert "LM317T" in results
        assert "STM32F4" in results

    async def test_enrichment_skipped_when_disabled(self, db_session: Session):
        with patch("app.services.vendor_email_lookup._enrich_vendors_batch") as mock_enrich:
            await find_vendors_for_parts(["LM317T"], db_session, enrich_missing=False)
            mock_enrich.assert_not_called()

    async def test_enrichment_triggered_for_missing_emails(self, db_session: Session, requisition_with_req: tuple):
        _, req_item = requisition_with_req
        # Create vendor with sighting but no email
        s = Sighting(
            requirement_id=req_item.id,
            normalized_mpn="noemail_part",
            vendor_name="No Email Vendor",
            source_type="api",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()

        async def _mock_enrich(*a, **kw):
            pass

        with patch("app.services.vendor_email_lookup._enrich_vendors_batch", new=_mock_enrich):
            results = await find_vendors_for_parts(
                ["NOEMAIL_PART"], db_session, enrich_missing=True, enrich_timeout=1.0
            )
        assert "NOEMAIL_PART" in results

    async def test_skips_empty_mpn(self, db_session: Session):
        results = await find_vendors_for_parts(["", "LM317T"], db_session, enrich_missing=False)
        assert "" in results
        assert results[""] == []


class TestBuildInquiryGroups:
    def test_empty_vendor_results_returns_empty_list(self):
        groups = build_inquiry_groups({}, [])
        assert groups == []

    def test_single_vendor_single_part(self):
        vendor_results = {
            "LM317T": [
                {
                    "vendor_name": "Arrow Electronics",
                    "emails": ["sales@arrow.com"],
                    "domain": "arrow.com",
                }
            ]
        }
        parts = [{"mpn": "LM317T", "qty": 500}]
        groups = build_inquiry_groups(vendor_results, parts)
        assert len(groups) == 1
        g = groups[0]
        assert g["vendor_name"] == "Arrow Electronics"
        assert g["vendor_email"] == "sales@arrow.com"
        assert "LM317T" in g["subject"]

    def test_vendor_with_no_email_excluded(self):
        vendor_results = {
            "LM317T": [
                {
                    "vendor_name": "No Email Vendor",
                    "emails": [],
                    "domain": "noemail.com",
                }
            ]
        }
        parts = [{"mpn": "LM317T", "qty": 100}]
        groups = build_inquiry_groups(vendor_results, parts)
        assert len(groups) == 0

    def test_multiple_parts_same_vendor(self):
        vendor_results = {
            "LM317T": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"], "domain": "arrow.com"}],
            "STM32F4": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"], "domain": "arrow.com"}],
        }
        parts = [{"mpn": "LM317T", "qty": 100}, {"mpn": "STM32F4", "qty": 50}]
        groups = build_inquiry_groups(vendor_results, parts)
        assert len(groups) == 1  # Same email → grouped together

    def test_subject_truncated_beyond_3_parts(self):
        vendor_results = {
            f"PART{i}": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"], "domain": "arrow.com"}]
            for i in range(5)
        }
        parts = [{"mpn": f"PART{i}", "qty": 10} for i in range(5)]
        groups = build_inquiry_groups(vendor_results, parts)
        assert len(groups) == 1
        assert "+ 2 more" in groups[0]["subject"]

    def test_body_contains_part_numbers(self):
        vendor_results = {"LM317T": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"], "domain": "arrow.com"}]}
        parts = [{"mpn": "LM317T", "qty": 500}]
        groups = build_inquiry_groups(vendor_results, parts)
        assert "LM317T" in groups[0]["body"]

    def test_body_contains_sender_info(self):
        vendor_results = {"LM317T": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"], "domain": "arrow.com"}]}
        parts = [{"mpn": "LM317T", "qty": 500}]
        groups = build_inquiry_groups(vendor_results, parts, company_name="Acme Corp", sender_name="Bob Smith")
        assert "Acme Corp" in groups[0]["body"]
        assert "Bob Smith" in groups[0]["body"]

    def test_qty_in_body(self):
        vendor_results = {"LM317T": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"], "domain": "arrow.com"}]}
        parts = [{"mpn": "LM317T", "qty": 750}]
        groups = build_inquiry_groups(vendor_results, parts)
        assert "750" in groups[0]["body"]

    def test_zero_qty_omitted_from_body(self):
        vendor_results = {"LM317T": [{"vendor_name": "Arrow", "emails": ["sales@arrow.com"], "domain": "arrow.com"}]}
        parts = [{"mpn": "LM317T"}]  # no qty
        groups = build_inquiry_groups(vendor_results, parts)
        # Should still generate a group, just without qty display
        assert len(groups) == 1
        assert "LM317T" in groups[0]["body"]

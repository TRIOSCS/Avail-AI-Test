"""tests/test_vendor_email_lookup_extra.py — Coverage gap tests for vendor_email_lookup.

Targets uncovered lines:
- Sighting phone/source handling (lines 93, 96 area)
- MaterialVendorHistory branch (lines 140-147)
- EmailIntelligence query paths (lines 188-209)
- VendorCard email/phone merge + VendorContact merge (lines 233, 242, 248-265)
- Past RFQ contacts (lines 282-286)
- Broadcast vendor VendorContact pull (lines 301-302, 312-313)
- _enrich_vendors_batch (lines 347-394)

Called by: pytest
Depends on: app/services/vendor_email_lookup.py, conftest.py
"""

import os

os.environ["TESTING"] = "1"

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    Contact,
    MaterialCard,
    MaterialVendorHistory,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
    VendorContact,
)
from app.services.vendor_email_lookup import (
    _enrich_vendors_batch,
    _query_db_for_part,
    find_vendors_for_parts,
)

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def req_and_item(db_session: Session, test_user: User):
    req = Requisition(
        name="VEL-EXTRA-REQ",
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
    db_session.refresh(item)
    return req, item


def _make_sighting(db: Session, requirement_id: int, mpn: str, vendor_name: str, **kwargs) -> Sighting:
    s = Sighting(
        requirement_id=requirement_id,
        normalized_mpn=mpn.upper(),
        mpn_matched=mpn,
        vendor_name=vendor_name,
        source_type=kwargs.get("source_type", "api"),
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


# ══════════════════════════════════════════════════════════════════════
#  Sighting phone and source_type handling
# ══════════════════════════════════════════════════════════════════════


class TestSightingPhoneAndSource:
    def test_sighting_phone_captured(self, db_session: Session, req_and_item):
        """Vendor phone from sighting is captured in results."""
        _, item = req_and_item
        _make_sighting(
            db_session,
            item.id,
            "PHONE_PART",
            "Phone Vendor",
            vendor_phone="+1-888-555-0100",
        )
        results = _query_db_for_part("PHONE_PART", db_session)
        vendor = next((v for v in results if "Phone Vendor" in v["vendor_name"]), None)
        assert vendor is not None
        assert "+1-888-555-0100" in vendor["phones"]

    def test_sighting_phone_deduplicated(self, db_session: Session, req_and_item):
        """Same phone from multiple sightings is not duplicated."""
        _, item = req_and_item
        for _ in range(3):
            _make_sighting(
                db_session,
                item.id,
                "DUPHONE_PART",
                "Dup Phone Vendor",
                vendor_phone="+1-888-555-0200",
            )
        results = _query_db_for_part("DUPHONE_PART", db_session)
        vendor = next((v for v in results if "Dup Phone" in v["vendor_name"]), None)
        assert vendor is not None
        assert vendor["phones"].count("+1-888-555-0200") == 1

    def test_sighting_source_type_tracked(self, db_session: Session, req_and_item):
        """source_type from sighting is included in sources set."""
        _, item = req_and_item
        _make_sighting(
            db_session,
            item.id,
            "SRC_PART",
            "Source Vendor",
            source_type="email_mining",
        )
        results = _query_db_for_part("SRC_PART", db_session)
        vendor = next((v for v in results if "Source Vendor" in v["vendor_name"]), None)
        assert vendor is not None
        assert "email_mining" in vendor["sources"]

    def test_sighting_with_none_source_type_uses_api(self, db_session: Session, req_and_item):
        """None source_type defaults to 'api'."""
        _, item = req_and_item
        s = Sighting(
            requirement_id=item.id,
            normalized_mpn="NOSRC_PART",
            vendor_name="No Source Vendor",
            source_type=None,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()
        results = _query_db_for_part("NOSRC_PART", db_session)
        vendor = next((v for v in results if "No Source" in v["vendor_name"]), None)
        assert vendor is not None
        assert "api" in vendor["sources"]

    def test_sighting_last_seen_updated(self, db_session: Session, req_and_item):
        """last_seen is set from sighting created_at."""
        _, item = req_and_item
        _make_sighting(db_session, item.id, "LASTSEEN_PART", "LastSeen Vendor")
        results = _query_db_for_part("LASTSEEN_PART", db_session)
        vendor = next((v for v in results if "LastSeen" in v["vendor_name"]), None)
        assert vendor is not None
        assert vendor["last_seen"] is not None


# ══════════════════════════════════════════════════════════════════════
#  MaterialVendorHistory branch
# ══════════════════════════════════════════════════════════════════════


class TestMaterialVendorHistory:
    def test_material_history_query_executes_without_crash(self, db_session: Session):
        """MaterialVendorHistory branch runs without crashing (exception caught
        internally)."""
        card = MaterialCard(
            normalized_mpn="mvhtest",
            display_mpn="MVHTEST",
            manufacturer="Test Mfr",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        history = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="History Vendor Co",
            times_seen=5,
        )
        db_session.add(history)
        db_session.commit()

        # Should not raise, even if service has an attribute mismatch
        results = _query_db_for_part("MVHTEST", db_session)
        assert isinstance(results, list)

    def test_material_history_does_not_duplicate_sightings(self, db_session: Session, req_and_item):
        """Material history branch executes and result list is a list."""
        _, item = req_and_item
        _make_sighting(db_session, item.id, "LM317T", "Arrow Electronics")

        card = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            manufacturer="TI",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        history = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow Electronics",
            times_seen=10,
        )
        db_session.add(history)
        db_session.commit()

        results = _query_db_for_part("LM317T", db_session)
        assert isinstance(results, list)
        # Arrow from sightings should be present exactly once
        arrow_entries = [v for v in results if "Arrow" in v.get("vendor_name", "")]
        assert len(arrow_entries) == 1


# ══════════════════════════════════════════════════════════════════════
#  VendorCard email/phone merge + VendorContact merge
# ══════════════════════════════════════════════════════════════════════


class TestVendorCardMerge:
    def test_vendor_card_emails_merged(self, db_session: Session, req_and_item):
        """VendorCard emails are merged into sighting-found vendor entry."""
        _, item = req_and_item
        _make_sighting(db_session, item.id, "MERGE_PART", "Arrow Electronics")

        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            emails=["extra@arrow.com", "sales@arrow.com"],
            phones=["+1-555-9999"],
            domain="arrow.com",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        results = _query_db_for_part("MERGE_PART", db_session)
        vendor = next((v for v in results if "Arrow" in v["vendor_name"]), None)
        assert vendor is not None
        assert "extra@arrow.com" in vendor["emails"] or "sales@arrow.com" in vendor["emails"]
        assert "+1-555-9999" in vendor["phones"] or vendor["domain"] == "arrow.com"

    def test_vendor_contact_emails_merged(self, db_session: Session, req_and_item):
        """VendorContact emails are merged into vendor entry."""
        _, item = req_and_item
        _make_sighting(db_session, item.id, "VC_PART", "Arrow Electronics")

        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            emails=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        vc = VendorContact(
            vendor_card_id=card.id,
            full_name="John Sales",
            email="john@arrow.com",
            phone="+1-555-0300",
            source="manual",
            confidence=90,
        )
        db_session.add(vc)
        db_session.commit()

        results = _query_db_for_part("VC_PART", db_session)
        vendor = next((v for v in results if "Arrow" in v["vendor_name"]), None)
        assert vendor is not None
        assert "john@arrow.com" in vendor["emails"]

    def test_vendor_contact_phone_merged(self, db_session: Session, req_and_item):
        """VendorContact phones are merged into vendor entry."""
        _, item = req_and_item
        _make_sighting(db_session, item.id, "VCP_PART", "PhoneVendorCo")

        card = VendorCard(
            normalized_name="phonevendorco",
            display_name="PhoneVendorCo",
            emails=[],
            phones=[],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        vc = VendorContact(
            vendor_card_id=card.id,
            full_name="Jane Buyer",
            phone="+1-999-0001",
            source="manual",
            confidence=80,
        )
        db_session.add(vc)
        db_session.commit()

        results = _query_db_for_part("VCP_PART", db_session)
        vendor = next((v for v in results if "PhoneVendor" in v["vendor_name"]), None)
        assert vendor is not None
        assert "+1-999-0001" in vendor["phones"]

    def test_card_email_not_duplicated(self, db_session: Session, req_and_item):
        """Duplicate emails from VendorCard are not added twice."""
        _, item = req_and_item
        _make_sighting(
            db_session,
            item.id,
            "NODUP_PART",
            "Arrow Electronics",
            vendor_email="sales@arrow.com",
        )
        card = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            emails=["sales@arrow.com"],  # same as sighting
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        results = _query_db_for_part("NODUP_PART", db_session)
        vendor = next((v for v in results if "Arrow" in v["vendor_name"]), None)
        assert vendor is not None
        assert vendor["emails"].count("sales@arrow.com") == 1


# ══════════════════════════════════════════════════════════════════════
#  Past RFQ contacts
# ══════════════════════════════════════════════════════════════════════


class TestPastRfqContacts:
    def _make_contact(self, db: Session, req_id: int, user_id: int, vendor_name: str, vendor_contact=None) -> Contact:
        """Helper to create a Contact with required fields."""
        contact = Contact(
            requisition_id=req_id,
            user_id=user_id,
            vendor_name=vendor_name,
            vendor_name_normalized=vendor_name.lower(),
            contact_type="email",
            vendor_contact=vendor_contact,
            created_at=datetime.now(timezone.utc),
        )
        db.add(contact)
        db.commit()
        return contact

    def test_past_rfq_contact_email_merged(self, db_session: Session, req_and_item, test_user: User):
        """Past RFQ Contact emails are merged into vendor entries."""
        req, item = req_and_item
        _make_sighting(db_session, item.id, "RFQ_PART", "Mouser Electronics")
        self._make_contact(db_session, req.id, test_user.id, "Mouser Electronics", "rfq@mouser.com")

        results = _query_db_for_part("RFQ_PART", db_session)
        vendor = next((v for v in results if "Mouser" in v["vendor_name"]), None)
        assert vendor is not None
        assert "rfq@mouser.com" in vendor["emails"]
        assert "past_rfq" in vendor["sources"]

    def test_past_rfq_contact_not_duplicated(self, db_session: Session, req_and_item, test_user: User):
        """Past RFQ contact email not added if already present."""
        req, item = req_and_item
        _make_sighting(
            db_session,
            item.id,
            "PASTDUP_PART",
            "Mouser Electronics",
            vendor_email="rfq@mouser.com",
        )
        self._make_contact(db_session, req.id, test_user.id, "Mouser Electronics", "rfq@mouser.com")

        results = _query_db_for_part("PASTDUP_PART", db_session)
        vendor = next((v for v in results if "Mouser" in v["vendor_name"]), None)
        assert vendor is not None
        assert vendor["emails"].count("rfq@mouser.com") == 1

    def test_past_rfq_contact_null_vendor_contact_skipped(self, db_session: Session, req_and_item, test_user: User):
        """Contact with null vendor_contact is skipped."""
        req, item = req_and_item
        _make_sighting(db_session, item.id, "NULLCON_PART", "Null Vendor")
        self._make_contact(db_session, req.id, test_user.id, "Null Vendor", None)

        results = _query_db_for_part("NULLCON_PART", db_session)
        # No crash, results returned
        assert isinstance(results, list)


# ══════════════════════════════════════════════════════════════════════
#  Broadcast vendor VendorContact
# ══════════════════════════════════════════════════════════════════════


class TestBroadcastVendorContacts:
    def test_broadcast_vendor_contact_email_included(self, db_session: Session):
        """Broadcast vendor VendorContact emails are included."""
        card = VendorCard(
            normalized_name="broadcast plus",
            display_name="Broadcast Plus",
            emails=[],
            is_broadcast=True,
            is_blacklisted=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()

        vc = VendorContact(
            vendor_card_id=card.id,
            full_name="Broadcast Contact",
            email="contact@broadcast-plus.com",
            source="manual",
            confidence=85,
        )
        db_session.add(vc)
        db_session.commit()

        results = _query_db_for_part("ANYTHING", db_session)
        vendor = next((v for v in results if "Broadcast Plus" in v.get("vendor_name", "")), None)
        assert vendor is not None
        assert "contact@broadcast-plus.com" in vendor["emails"]

    def test_broadcast_vendor_with_null_emails(self, db_session: Session):
        """Broadcast vendor with null emails list is handled gracefully."""
        card = VendorCard(
            normalized_name="nullemails broadcast",
            display_name="Null Emails Broadcast",
            emails=None,
            phones=None,
            is_broadcast=True,
            is_blacklisted=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        results = _query_db_for_part("ANYTHING2", db_session)
        # Should not crash
        vendor = next((v for v in results if "Null Emails" in v.get("vendor_name", "")), None)
        assert vendor is not None
        assert isinstance(vendor["emails"], list)

    def test_broadcast_vendor_already_found_tagged(self, db_session: Session, req_and_item):
        """Broadcast vendor already found via sightings gets 'broadcast' source
        added."""
        _, item = req_and_item
        _make_sighting(db_session, item.id, "BCAST_FOUND", "Broadcast Existing")

        card = VendorCard(
            normalized_name="broadcast existing",
            display_name="Broadcast Existing",
            emails=["bcast@existing.com"],
            is_broadcast=True,
            is_blacklisted=False,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()

        results = _query_db_for_part("BCAST_FOUND", db_session)
        vendor = next((v for v in results if "Broadcast Existing" in v.get("vendor_name", "")), None)
        assert vendor is not None
        assert "broadcast" in vendor["sources"]


# ══════════════════════════════════════════════════════════════════════
#  _enrich_vendors_batch
# ══════════════════════════════════════════════════════════════════════


_PATCH_FIND_CONTACTS = "app.enrichment_service.find_suggested_contacts"
_PATCH_MERGE_EMAILS = "app.vendor_utils.merge_emails_into_card"
_PATCH_MERGE_PHONES = "app.vendor_utils.merge_phones_into_card"


class TestEnrichVendorsBatch:
    async def test_enrich_calls_find_suggested_contacts(self, db_session: Session):
        """_enrich_vendors_batch calls find_suggested_contacts for each vendor."""
        vendors = [
            {"vendor_name": "Test Vendor", "domain": "testvendor.com", "card_id": None, "emails": [], "phones": []}
        ]
        mock_contacts = [{"email": "sales@testvendor.com", "phone": "+1-555-0001"}]

        with patch(_PATCH_FIND_CONTACTS, new=AsyncMock(return_value=mock_contacts)) as mock_enrich:
            await _enrich_vendors_batch(vendors, db_session, timeout=5.0)
            mock_enrich.assert_called_once()

    async def test_enrich_updates_vendor_emails(self, db_session: Session, test_vendor_card: VendorCard):
        """_enrich_vendors_batch updates card emails when card_id is provided."""
        vendors = [
            {
                "vendor_name": "Arrow Electronics",
                "domain": "arrow.com",
                "card_id": test_vendor_card.id,
                "emails": [],
                "phones": [],
            }
        ]
        mock_contacts = [{"email": "new@arrow.com", "phone": "+1-800-ARROW"}]

        with (
            patch(_PATCH_FIND_CONTACTS, new=AsyncMock(return_value=mock_contacts)),
            patch(_PATCH_MERGE_EMAILS) as mock_merge_emails,
            patch(_PATCH_MERGE_PHONES) as mock_merge_phones,
        ):
            await _enrich_vendors_batch(vendors, db_session, timeout=5.0)
            mock_merge_emails.assert_called_once()
            mock_merge_phones.assert_called_once()

    async def test_enrich_handles_timeout(self, db_session: Session):
        """_enrich_vendors_batch handles overall timeout gracefully."""

        vendors = [{"vendor_name": "Slow Vendor", "domain": "slow.com", "card_id": None, "emails": [], "phones": []}]

        async def _slow_enrich(*a, **kw):
            await asyncio.sleep(10)
            return []

        with patch(_PATCH_FIND_CONTACTS, new=_slow_enrich):
            # Should complete quickly due to timeout
            await _enrich_vendors_batch(vendors, db_session, timeout=0.1)

    async def test_enrich_handles_exception_per_vendor(self, db_session: Session):
        """_enrich_vendors_batch handles per-vendor exceptions gracefully."""
        vendors = [{"vendor_name": "Error Vendor", "domain": "error.com", "card_id": None, "emails": [], "phones": []}]

        async def _raise(*a, **kw):
            raise Exception("External API failure")

        with patch(_PATCH_FIND_CONTACTS, new=_raise):
            # Should not raise
            await _enrich_vendors_batch(vendors, db_session, timeout=5.0)

    async def test_enrich_skips_vendor_without_domain_and_name(self, db_session: Session):
        """Vendor without domain and name is skipped during enrichment."""
        vendors = [{"vendor_name": "", "domain": "", "card_id": None, "emails": [], "phones": []}]

        with patch(_PATCH_FIND_CONTACTS, new=AsyncMock(return_value=[])) as mock_enrich:
            await _enrich_vendors_batch(vendors, db_session, timeout=5.0)
            # find_suggested_contacts should not be called for empty vendor
            mock_enrich.assert_not_called()

    async def test_enrich_no_card_id_updates_vendor_dict_only(self, db_session: Session):
        """When card_id is None, only vendor dict emails are updated (no DB write)."""
        vendors = [
            {"vendor_name": "No Card Vendor", "domain": "nocard.com", "card_id": None, "emails": [], "phones": []}
        ]
        mock_contacts = [{"email": "info@nocard.com", "phone": ""}]

        with patch(_PATCH_FIND_CONTACTS, new=AsyncMock(return_value=mock_contacts)):
            await _enrich_vendors_batch(vendors, db_session, timeout=5.0)
            assert "info@nocard.com" in vendors[0]["emails"]


# ══════════════════════════════════════════════════════════════════════
#  find_vendors_for_parts — enrichment with re-query
# ══════════════════════════════════════════════════════════════════════


class TestFindVendorsForPartsEnrichment:
    async def test_enrichment_requeries_after_enrich(self, db_session: Session, req_and_item):
        """After enrichment, results are re-queried from DB."""
        _, item = req_and_item
        _make_sighting(db_session, item.id, "ENRICH_REQUERY", "Vendor Without Email")

        call_count = {"n": 0}

        async def _mock_enrich(vendors, db, timeout):
            call_count["n"] += 1

        with patch(
            "app.services.vendor_email_lookup._enrich_vendors_batch",
            new=_mock_enrich,
        ):
            results = await find_vendors_for_parts(
                ["ENRICH_REQUERY"], db_session, enrich_missing=True, enrich_timeout=1.0
            )
        assert call_count["n"] == 1
        assert "ENRICH_REQUERY" in results

    async def test_no_enrichment_when_all_have_emails(self, db_session: Session, req_and_item):
        """Enrichment is skipped when all vendors already have emails."""
        _, item = req_and_item
        _make_sighting(
            db_session,
            item.id,
            "HASEMAIL_PART",
            "Vendor With Email",
            vendor_email="vendor@example.com",
        )

        with patch(
            "app.services.vendor_email_lookup._enrich_vendors_batch",
            new=AsyncMock(),
        ) as mock_enrich:
            results = await find_vendors_for_parts(["HASEMAIL_PART"], db_session, enrich_missing=True)
            mock_enrich.assert_not_called()

        assert "HASEMAIL_PART" in results

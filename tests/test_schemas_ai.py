"""
test_schemas_ai.py — Tests for app/schemas/ai.py

Called by: pytest
Depends on: app/schemas/ai.py
"""

import pytest
from pydantic import ValidationError

from app.schemas.ai import (
    DraftOfferItem,
    ProspectContactSave,
    ProspectFinderRequest,
    RfqDraftRequest,
    SaveDraftOffersRequest,
)


class TestProspectFinderRequest:
    def test_defaults(self):
        r = ProspectFinderRequest()
        assert r.entity_type == "company" and r.entity_id is None

    def test_vendor_type(self):
        r = ProspectFinderRequest(entity_type="vendor", entity_id=42)
        assert r.entity_type == "vendor"

    def test_invalid_entity_type_raises(self):
        with pytest.raises(ValidationError):
            ProspectFinderRequest(entity_type="bogus")


class TestProspectContactSave:
    def test_empty(self):
        assert ProspectContactSave().notes is None

    def test_with_notes(self):
        assert ProspectContactSave(notes="keep this").notes == "keep this"


class TestSaveDraftOffersRequest:
    def test_valid(self):
        r = SaveDraftOffersRequest(
            requisition_id=1,
            offers=[DraftOfferItem(vendor_name="Acme", mpn="ABC123")],
        )
        assert len(r.offers) == 1

    def test_missing_req_id_raises(self):
        with pytest.raises(ValidationError):
            SaveDraftOffersRequest(offers=[DraftOfferItem()])

    def test_empty_offers_raises(self):
        with pytest.raises(ValidationError):
            SaveDraftOffersRequest(requisition_id=1, offers=[])

    def test_zero_req_id_raises(self):
        with pytest.raises(ValidationError, match="positive"):
            SaveDraftOffersRequest(
                requisition_id=0,
                offers=[DraftOfferItem()],
            )


class TestRfqDraftRequest:
    def test_valid(self):
        r = RfqDraftRequest(vendor_name="Acme", parts=["LM358"])
        assert r.vendor_name == "Acme"

    def test_blank_vendor_raises(self):
        with pytest.raises(ValidationError, match="vendor_name required"):
            RfqDraftRequest(vendor_name="  ", parts=["LM358"])

    def test_empty_parts_raises(self):
        with pytest.raises(ValidationError):
            RfqDraftRequest(vendor_name="Acme", parts=[])

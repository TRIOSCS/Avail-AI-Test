"""
test_schemas_ai.py — Tests for app/schemas/ai.py

Called by: pytest
Depends on: app/schemas/ai.py
"""

import pytest
from pydantic import ValidationError

from app.schemas.ai import (
    ApplyFreeformRfqRequest,
    DraftOfferItem,
    ParseFreeformOfferRequest,
    ParseFreeformRfqRequest,
    ProspectContactSave,
    ProspectFinderRequest,
    RfqDraftRequest,
    SaveDraftOffersRequest,
    SaveFreeformOffersRequest,
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


class TestParseFreeformRfqRequest:
    def test_valid(self):
        payload = ParseFreeformRfqRequest(raw_text="Need 500 pcs of LM317T")
        assert payload.raw_text == "Need 500 pcs of LM317T"

    def test_empty_text_raises(self):
        with pytest.raises(ValidationError):
            ParseFreeformRfqRequest(raw_text="")


class TestParseFreeformOfferRequest:
    def test_defaults(self):
        payload = ParseFreeformOfferRequest(raw_text="LM317T 500 @ $0.45")
        assert payload.requisition_id is None

    def test_with_requisition_id(self):
        payload = ParseFreeformOfferRequest(raw_text="LM317T 500 @ $0.45", requisition_id=7)
        assert payload.requisition_id == 7


class TestApplyFreeformRfqRequest:
    def test_valid(self):
        payload = ApplyFreeformRfqRequest(
            name="Acme Intake RFQ",
            customer_site_id=3,
            requirements=[{"primary_mpn": "LM317T", "target_qty": 500}],
        )
        assert payload.name == "Acme Intake RFQ"
        assert payload.customer_site_id == 3

    def test_empty_requirements_raises(self):
        with pytest.raises(ValidationError):
            ApplyFreeformRfqRequest(name="Acme Intake RFQ", customer_site_id=3, requirements=[])


class TestSaveFreeformOffersRequest:
    def test_valid(self):
        payload = SaveFreeformOffersRequest(
            requisition_id=1,
            offers=[DraftOfferItem(vendor_name="Acme", mpn="LM317T")],
        )
        assert payload.requisition_id == 1
        assert len(payload.offers) == 1

    def test_zero_req_id_raises(self):
        with pytest.raises(ValidationError):
            SaveFreeformOffersRequest(
                requisition_id=0,
                offers=[DraftOfferItem(vendor_name="Acme", mpn="LM317T")],
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


# ── Additional coverage for missing lines ───────────────────────────

from app.schemas.ai import (
    CompareQuotesRequest,
    NormalizedPart,
    NormalizePartsRequest,
    ParsedQuote,
    ParseEmailRequest,
    ParseEmailResponse,
    QuoteForAnalysis,
    RfqDraftEmailRequest,
    RfqDraftPart,
)


class TestDraftOfferItemValidators:
    def test_condition_none_passes(self):
        d = DraftOfferItem(condition=None)
        assert d.condition is None

    def test_condition_normalized(self):
        d = DraftOfferItem(condition="Factory New")
        assert d.condition == "new"

    def test_packaging_none_passes(self):
        d = DraftOfferItem(packaging=None)
        assert d.packaging is None

    def test_packaging_normalized(self):
        d = DraftOfferItem(packaging="Tape & Reel")
        assert d.packaging == "reel"

    def test_mpn_empty_passes(self):
        d = DraftOfferItem(mpn="")
        assert d.mpn == ""

    def test_mpn_normalized(self):
        d = DraftOfferItem(mpn="lm317t")
        assert d.mpn == "LM317T"


class TestParseEmailRequest:
    def test_valid(self):
        r = ParseEmailRequest(email_body="Here is the quote...")
        assert r.email_body == "Here is the quote..."
        assert r.email_subject == ""
        assert r.vendor_name == ""


class TestParsedQuote:
    def test_defaults(self):
        q = ParsedQuote()
        assert q.confidence == 0.5
        assert q.currency == "USD"


class TestParseEmailResponse:
    def test_defaults(self):
        r = ParseEmailResponse(parsed=True)
        assert r.quotes == []
        assert r.overall_confidence == 0.0


class TestNormalizePartsRequest:
    def test_valid(self):
        r = NormalizePartsRequest(parts=["LM317T"])
        assert len(r.parts) == 1

    def test_empty_raises(self):
        with pytest.raises(ValidationError):
            NormalizePartsRequest(parts=[])


class TestNormalizedPart:
    def test_defaults(self):
        p = NormalizedPart(original="lm317t", normalized="LM317T")
        assert p.is_alias is False
        assert p.confidence == 0.0


class TestRfqDraftPart:
    def test_valid(self):
        p = RfqDraftPart(part_number="LM317T", quantity=1000)
        assert p.manufacturer is None
        assert p.target_price is None


class TestRfqDraftEmailRequest:
    def test_valid(self):
        r = RfqDraftEmailRequest(
            vendor_name="Arrow",
            buyer_name="John",
            parts=[RfqDraftPart(part_number="LM317T", quantity=1000)],
        )
        assert r.vendor_contact_name is None


class TestQuoteForAnalysis:
    def test_defaults(self):
        q = QuoteForAnalysis(vendor_name="Arrow")
        assert q.currency == "USD"
        assert q.vendor_score is None


class TestCompareQuotesRequest:
    def test_valid(self):
        r = CompareQuotesRequest(
            part_number="LM317T",
            quotes=[
                QuoteForAnalysis(vendor_name="A"),
                QuoteForAnalysis(vendor_name="B"),
            ],
        )
        assert r.required_qty is None

    def test_too_few_quotes_raises(self):
        with pytest.raises(ValidationError):
            CompareQuotesRequest(
                part_number="LM317T",
                quotes=[QuoteForAnalysis(vendor_name="A")],
            )

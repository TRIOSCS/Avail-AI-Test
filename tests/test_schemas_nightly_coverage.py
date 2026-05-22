"""test_schemas_nightly_coverage.py — Tests for app/schemas/proactive.py and app/schemas/admin.py.

Covers Pydantic model instantiation and field validation.

Called by: pytest
Depends on: app/schemas/proactive.py, app/schemas/admin.py
"""

import os

os.environ["TESTING"] = "1"

from app.schemas.admin import SourceCredentialsUpdate, TeamsChannelRouting
from app.schemas.proactive import (
    DismissMatches,
    DoNotOfferItem,
    DoNotOfferRequest,
    DraftProactive,
    SendProactive,
)


class TestDismissMatches:
    def test_basic(self):
        obj = DismissMatches(match_ids=[1, 2, 3])
        assert obj.match_ids == [1, 2, 3]

    def test_empty_list(self):
        obj = DismissMatches(match_ids=[])
        assert obj.match_ids == []


class TestDraftProactive:
    def test_defaults(self):
        obj = DraftProactive(match_ids=[1])
        assert obj.match_ids == [1]
        assert obj.contact_ids == []
        assert obj.sell_prices == {}
        assert obj.notes is None

    def test_full(self):
        obj = DraftProactive(
            match_ids=[1, 2],
            contact_ids=[10],
            sell_prices={"LM317T": 0.55},
            notes="Draft note",
        )
        assert obj.notes == "Draft note"
        assert obj.sell_prices["LM317T"] == 0.55


class TestSendProactive:
    def test_required_fields(self):
        obj = SendProactive(match_ids=[1], contact_ids=[10])
        assert obj.match_ids == [1]
        assert obj.contact_ids == [10]
        assert obj.sell_prices == {}
        assert obj.subject is None
        assert obj.notes is None
        assert obj.email_html is None

    def test_full(self):
        obj = SendProactive(
            match_ids=[1, 2],
            contact_ids=[10, 11],
            sell_prices={"ABC": 1.25},
            subject="RFQ for ABC",
            notes="Please respond",
            email_html="<p>Hello</p>",
        )
        assert obj.subject == "RFQ for ABC"
        assert obj.email_html == "<p>Hello</p>"


class TestDoNotOfferItem:
    def test_required(self):
        obj = DoNotOfferItem(mpn="LM317T", company_id=42)
        assert obj.mpn == "LM317T"
        assert obj.company_id == 42
        assert obj.reason is None

    def test_with_reason(self):
        obj = DoNotOfferItem(mpn="LM317T", company_id=42, reason="Competitor")
        assert obj.reason == "Competitor"


class TestDoNotOfferRequest:
    def test_empty(self):
        obj = DoNotOfferRequest(items=[])
        assert obj.items == []

    def test_with_items(self):
        obj = DoNotOfferRequest(
            items=[
                DoNotOfferItem(mpn="LM317T", company_id=1),
                DoNotOfferItem(mpn="TL071", company_id=2, reason="Price"),
            ]
        )
        assert len(obj.items) == 2


class TestSourceCredentialsUpdate:
    def test_accepts_dynamic_keys(self):
        obj = SourceCredentialsUpdate(**{"API_KEY": "secret123", "API_SECRET": "xyz"})
        assert obj.model_extra["API_KEY"] == "secret123"
        assert obj.model_extra["API_SECRET"] == "xyz"

    def test_empty(self):
        obj = SourceCredentialsUpdate()
        assert obj.model_extra == {}


class TestTeamsChannelRouting:
    def test_accepts_dynamic_keys(self):
        obj = TeamsChannelRouting(
            teams_channel_hot="https://webhook.office.com/hot",
            teams_channel_quotes="https://webhook.office.com/quotes",
        )
        assert "teams_channel_hot" in obj.model_extra

    def test_empty(self):
        obj = TeamsChannelRouting()
        assert obj.model_extra == {}

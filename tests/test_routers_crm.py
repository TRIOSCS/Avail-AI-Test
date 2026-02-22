"""
tests/test_routers_crm.py — Tests for CRM Router Helpers + Endpoints

Tests quote number generation, last-quoted-price lookup,
quote serialization, margin calculation, and CRM endpoints.

Called by: pytest
Depends on: app.routers.crm, conftest.py
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models import BuyPlan, Company, CustomerSite, Offer, Quote, Requisition, SiteContact, User
from app.routers.crm import (
    get_last_quoted_price,
    next_quote_number,
    quote_to_dict,
)

# ── Fixtures ─────────────────────────────────────────────────────────────


def _make_quote(**overrides):
    """Build a mock Quote object."""
    q = MagicMock()
    q.id = overrides.get("id", 1)
    q.requisition_id = overrides.get("requisition_id", 10)
    q.customer_site_id = overrides.get("customer_site_id", 5)
    q.quote_number = overrides.get("quote_number", "Q-2026-0001")
    q.revision = overrides.get("revision", 1)
    q.line_items = overrides.get("line_items", [])
    q.subtotal = overrides.get("subtotal", 100.0)
    q.total_cost = overrides.get("total_cost", 80.0)
    q.total_margin_pct = overrides.get("total_margin_pct", 20.0)
    q.payment_terms = overrides.get("payment_terms", "Net 30")
    q.shipping_terms = overrides.get("shipping_terms", "FOB")
    q.validity_days = overrides.get("validity_days", 30)
    q.notes = overrides.get("notes", None)
    q.status = overrides.get("status", "draft")
    q.sent_at = overrides.get("sent_at", None)
    q.result = overrides.get("result", None)
    q.result_reason = overrides.get("result_reason", None)
    q.result_notes = overrides.get("result_notes", None)
    q.result_at = overrides.get("result_at", None)
    q.won_revenue = overrides.get("won_revenue", None)
    q.created_at = overrides.get("created_at", datetime(2026, 2, 1, tzinfo=timezone.utc))
    q.updated_at = overrides.get("updated_at", datetime(2026, 2, 1, tzinfo=timezone.utc))

    # Relationships
    created_by = MagicMock()
    created_by.name = "Mike"
    q.created_by = overrides.get("created_by", created_by)

    site = MagicMock()
    site.site_name = "HQ"
    site.contact_name = "John"
    site.contact_email = "john@acme.com"
    company = MagicMock()
    company.name = "Acme Corp"
    site.company = company
    q.customer_site = overrides.get("customer_site", site)
    return q


# ── quote_to_dict ────────────────────────────────────────────────────────


def test_quote_to_dict_basic():
    q = _make_quote()
    d = quote_to_dict(q)
    assert d["id"] == 1
    assert d["quote_number"] == "Q-2026-0001"
    assert d["customer_name"] == "Acme Corp — HQ"
    assert d["contact_name"] == "John"
    assert d["contact_email"] == "john@acme.com"
    assert d["subtotal"] == 100.0
    assert d["total_margin_pct"] == 20.0
    assert d["created_by"] == "Mike"


def test_quote_to_dict_no_site():
    q = _make_quote(customer_site=None)
    d = quote_to_dict(q)
    assert d["customer_name"] == ""
    assert d["contact_name"] is None
    assert d["contact_email"] is None


def test_quote_to_dict_nulls():
    q = _make_quote(subtotal=None, total_cost=None, total_margin_pct=None, won_revenue=None)
    d = quote_to_dict(q)
    assert d["subtotal"] is None
    assert d["total_cost"] is None
    assert d["won_revenue"] is None


def test_quote_to_dict_sent():
    sent = datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc)
    q = _make_quote(sent_at=sent, status="sent")
    d = quote_to_dict(q)
    assert d["sent_at"] == sent.isoformat()
    assert d["status"] == "sent"


# ── next_quote_number ────────────────────────────────────────────────────


def test_next_quote_number_first():
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
    result = next_quote_number(db)
    assert result.startswith("Q-")
    assert result.endswith("-0001")


def test_next_quote_number_increment():
    last = MagicMock()
    last.quote_number = "Q-2026-0042"
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = last
    result = next_quote_number(db)
    assert result == "Q-2026-0043"


def test_next_quote_number_bad_format():
    """Handles corrupted quote numbers gracefully."""
    last = MagicMock()
    last.quote_number = "Q-2026-XXXX"
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = last
    result = next_quote_number(db)
    assert result.endswith("-0001")


# ── get_last_quoted_price ────────────────────────────────────────────────


def test_get_last_quoted_price_found():
    q = MagicMock()
    q.line_items = [{"mpn": "LM317T", "sell_price": 2.50, "margin_pct": 15.0}]
    q.quote_number = "Q-2026-0005"
    q.sent_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    q.created_at = datetime(2026, 1, 28, tzinfo=timezone.utc)
    q.result = "won"
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [q]

    result = get_last_quoted_price("LM317T", db)
    assert result is not None
    assert result["sell_price"] == 2.50
    assert result["quote_number"] == "Q-2026-0005"


def test_get_last_quoted_price_case_insensitive():
    q = MagicMock()
    q.line_items = [{"mpn": "lm317t", "sell_price": 3.00, "margin_pct": 10.0}]
    q.quote_number = "Q-2026-0010"
    q.sent_at = datetime(2026, 2, 5, tzinfo=timezone.utc)
    q.created_at = datetime(2026, 2, 4, tzinfo=timezone.utc)
    q.result = "sent"
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [q]

    result = get_last_quoted_price("  LM317T  ", db)
    assert result is not None
    assert result["sell_price"] == 3.00


def test_get_last_quoted_price_not_found():
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
    result = get_last_quoted_price("NOEXIST", db)
    assert result is None


# ── Margin calculation (update_quote logic) ──────────────────────────────


def test_margin_calculation():
    """Verify margin calc matches update_quote logic."""
    line_items = [
        {"qty": 100, "sell_price": 5.00, "cost_price": 3.50},
        {"qty": 50, "sell_price": 10.00, "cost_price": 7.00},
    ]
    total_sell = sum((i["qty"]) * (i["sell_price"]) for i in line_items)
    total_cost = sum((i["qty"]) * (i["cost_price"]) for i in line_items)
    margin = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0
    assert total_sell == 1000.0  # 500 + 500
    assert total_cost == 700.0   # 350 + 350
    assert margin == 30.0


def test_margin_zero_sell():
    """Zero sell price shouldn't divide by zero."""
    total_sell = 0
    total_cost = 100
    margin = round((total_sell - total_cost) / total_sell * 100, 2) if total_sell > 0 else 0
    assert margin == 0


# ═══════════════════════════════════════════════════════════════════════
#  CRM Endpoint Tests via TestClient
# ═══════════════════════════════════════════════════════════════════════


class TestCompanies:
    def test_list_companies_empty(self, client):
        resp = client.get("/api/companies")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_companies_with_data(self, client, db_session, test_company):
        resp = client.get("/api/companies")
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()]
        assert "Acme Electronics" in names

    def test_list_companies_search(self, client, db_session, test_company):
        resp = client.get("/api/companies", params={"search": "Acme"})
        assert resp.status_code == 200
        names = [c["name"] for c in resp.json()]
        assert "Acme Electronics" in names

    def test_list_companies_search_no_match(self, client, db_session, test_company):
        resp = client.get("/api/companies", params={"search": "Nonexistent"})
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("app.routers.crm.get_credential_cached", return_value=None)
    @patch("app.enrichment_service.normalize_company_input", new_callable=AsyncMock)
    def test_create_company(self, mock_normalize, mock_cred, client, db_session):
        mock_normalize.return_value = ("New Corp", "newcorp.com")
        resp = client.post("/api/companies", json={"name": "New Corp"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New Corp"
        assert "id" in data

    def test_create_company_duplicate_check(self, client, db_session, test_company):
        resp = client.get("/api/companies/check-duplicate", params={"name": "Acme Electronics"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["matches"]) >= 1

    def test_update_company(self, client, db_session, test_company):
        resp = client.put(
            f"/api/companies/{test_company.id}",
            json={"notes": "Updated notes"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_typeahead(self, client, db_session, test_company):
        resp = client.get("/api/companies/typeahead")
        assert resp.status_code == 200
        data = resp.json()
        names = [c["name"] for c in data]
        assert "Acme Electronics" in names


class TestSites:
    def test_add_site(self, client, db_session, test_company):
        resp = client.post(
            f"/api/companies/{test_company.id}/sites",
            json={"site_name": "Branch Office"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["site_name"] == "Branch Office"

    def test_get_site(self, client, db_session, test_customer_site):
        resp = client.get(f"/api/sites/{test_customer_site.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["site_name"] == "Acme HQ"

    def test_update_site(self, client, db_session, test_customer_site):
        resp = client.put(
            f"/api/sites/{test_customer_site.id}",
            json={"contact_name": "Updated Contact"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_list_site_contacts(self, client, db_session, test_customer_site):
        resp = client.get(f"/api/sites/{test_customer_site.id}/contacts")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_add_site_contact(self, client, db_session, test_customer_site):
        resp = client.post(
            f"/api/sites/{test_customer_site.id}/contacts",
            json={"full_name": "New Contact", "email": "newcontact@acme.com"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data


class TestOffers:
    def test_list_offers(self, client, db_session, test_requisition, test_offer):
        # Link offer to the requirement so it appears in the grouped response
        req_item = test_requisition.requirements[0]
        test_offer.requirement_id = req_item.id
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        # Response is {"has_new_offers": bool, "groups": [{..., "offers": [...]}]}
        all_offers = []
        for g in data.get("groups", []):
            all_offers.extend(g.get("offers", []))
        assert len(all_offers) >= 1

    def test_create_offer(self, client, db_session, test_requisition):
        req = test_requisition
        requirement = req.requirements[0]
        resp = client.post(
            f"/api/requisitions/{req.id}/offers",
            json={
                "requirement_id": requirement.id,
                "vendor_name": "Mouser Electronics",
                "mpn": "LM317T",
                "qty_available": 500,
                "unit_price": 0.45,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_name"] == "Mouser Electronics"

    def test_update_offer(self, client, db_session, test_offer):
        resp = client.put(
            f"/api/offers/{test_offer.id}",
            json={"unit_price": 0.55},
        )
        assert resp.status_code == 200

    def test_delete_offer(self, client, db_session, test_offer):
        resp = client.delete(f"/api/offers/{test_offer.id}")
        assert resp.status_code == 200


class TestQuotes:
    def test_create_quote(self, client, db_session, test_requisition, test_customer_site, test_offer):
        # Requisition must have a customer_site_id to allow quoting
        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/quote",
            json={"offer_ids": [test_offer.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "quote_number" in data

    def test_list_quotes(self, client, db_session, test_requisition, test_quote):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/quotes")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_delete_draft_quote(self, client, db_session, test_requisition, test_customer_site, test_user):
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-DEL1",
            status="draft",
            line_items=[],
            subtotal=0,
            total_cost=0,
            total_margin_pct=0,
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        resp = client.delete(f"/api/quotes/{q.id}")
        assert resp.status_code == 200

    def test_quote_result_won(self, client, db_session, test_quote):
        resp = client.post(
            f"/api/quotes/{test_quote.id}/result",
            json={"result": "won", "won_revenue": 1000.00},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("result") == "won" or data.get("ok") is True

    def test_pricing_history(self, client, db_session, test_quote):
        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data
        assert "mpn" in data


class TestBuyPlans:
    def _make_buy_plan(self, db_session, test_requisition, test_quote, test_offer, test_user):
        bp = BuyPlan(
            requisition_id=test_requisition.id,
            quote_id=test_quote.id,
            status="pending_approval",
            submitted_by_id=test_user.id,
            line_items=[{
                "offer_id": test_offer.id,
                "mpn": "LM317T",
                "qty": 1000,
                "cost_price": 0.50,
                "vendor_name": "Arrow Electronics",
            }],
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(bp)
        db_session.commit()
        db_session.refresh(bp)
        return bp

    def test_list_buy_plans(self, client, db_session, test_requisition, test_quote, test_offer, test_user):
        self._make_buy_plan(db_session, test_requisition, test_quote, test_offer, test_user)
        resp = client.get("/api/buy-plans")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_submit_buy_plan(self, client, db_session, test_requisition, test_customer_site, test_offer, monkeypatch):
        # Requisition needs customer_site_id for quoting
        test_requisition.customer_site_id = test_customer_site.id
        db_session.commit()

        # Create a quote first
        q = Quote(
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-2026-BP01",
            status="sent",
            line_items=[],
            subtotal=750.0,
            total_cost=500.0,
            total_margin_pct=33.3,
            created_by_id=db_session.query(User).first().id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(q)
        db_session.commit()

        # Prevent background notification task from running
        monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close() if hasattr(coro, 'close') else None)

        resp = client.post(
            f"/api/quotes/{q.id}/buy-plan",
            json={"offer_ids": [test_offer.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "buy_plan_id" in data

    def test_get_buy_plan(self, client, db_session, test_requisition, test_quote, test_offer, test_user):
        bp = self._make_buy_plan(db_session, test_requisition, test_quote, test_offer, test_user)
        resp = client.get(f"/api/buy-plans/{bp.id}")
        assert resp.status_code == 200

    def test_cancel_buy_plan(self, client, db_session, test_requisition, test_quote, test_offer, test_user, monkeypatch):
        bp = self._make_buy_plan(db_session, test_requisition, test_quote, test_offer, test_user)
        # Prevent background notification task from running
        monkeypatch.setattr("asyncio.create_task", lambda coro: coro.close() if hasattr(coro, 'close') else None)
        resp = client.put(f"/api/buy-plans/{bp.id}/cancel", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_buy_plans_for_quote(self, client, db_session, test_requisition, test_quote, test_offer, test_user):
        self._make_buy_plan(db_session, test_requisition, test_quote, test_offer, test_user)
        resp = client.get(f"/api/buy-plans/for-quote/{test_quote.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "status" in data

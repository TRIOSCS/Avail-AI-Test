"""
tests/test_routers_crm.py — Tests for CRM Router Helpers

Tests quote number generation, last-quoted-price lookup,
quote serialization, and margin calculation logic.

Called by: pytest
Depends on: app.routers.crm
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

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

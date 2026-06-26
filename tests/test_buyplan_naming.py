"""test_buyplan_naming.py — the shared deal-card title + top-flag-reason helpers.

Covers:
- build_card_title assembles "{SO#} - {Customer} - {Owner} - {Type}" for each kind
- BP/SO/PO suffix is appended verbatim and per-kind
- Missing SO# / customer / owner each collapse to an em dash (never ragged)
- Unknown kind raises ValueError (loud wiring-mistake signal)
- summarize_top_flag returns the worst-severity flag's verbatim reason (Part 4)

Depends on: app/services/buyplan_naming.
"""

from __future__ import annotations

import pytest

from app.services.buyplan_naming import (
    CARD_KIND_BUY_PLAN,
    CARD_KIND_PO,
    CARD_KIND_SALES_ORDER,
    build_card_title,
    summarize_top_flag,
)

# ── build_card_title: suffix + owner per kind ─────────────────────────


def test_buy_plan_title_suffix_bp():
    """Buy-Plan card ends '- BP' with the Account Manager as Owner."""
    title = build_card_title(
        sales_order_number="TSO-1234",
        customer_name="Acme Electronics",
        owner_name="Jordan Sales",
        kind=CARD_KIND_BUY_PLAN,
    )
    assert title == "TSO-1234 - Acme Electronics - Jordan Sales - BP"


def test_sales_order_title_suffix_so():
    """SO-approval card ends '- SO' with the Account Manager as Owner."""
    title = build_card_title(
        sales_order_number="TSO-1234",
        customer_name="Acme Electronics",
        owner_name="Jordan Sales",
        kind=CARD_KIND_SALES_ORDER,
    )
    assert title == "TSO-1234 - Acme Electronics - Jordan Sales - SO"


def test_po_title_suffix_po_with_buyer_owner():
    """PO-approval card ends '- PO' with the Buyer as Owner (distinct from the AM)."""
    title = build_card_title(
        sales_order_number="TSO-1234",
        customer_name="Acme Electronics",
        owner_name="Pat Buyer",  # the Buyer, not the sales owner
        kind=CARD_KIND_PO,
    )
    assert title == "TSO-1234 - Acme Electronics - Pat Buyer - PO"
    assert title.endswith(" - PO")


def test_all_three_kinds_share_one_prefix():
    """Same SO#/customer → identical prefix across all three kinds; only the suffix
    differs."""
    common = dict(sales_order_number="TSO-7", customer_name="Globex", owner_name="Sam")
    bp = build_card_title(kind=CARD_KIND_BUY_PLAN, **common)
    so = build_card_title(kind=CARD_KIND_SALES_ORDER, **common)
    po = build_card_title(kind=CARD_KIND_PO, **common)
    assert bp[:-2] == so[:-2] == po[:-2] == "TSO-7 - Globex - Sam - "
    assert (bp[-2:], so[-2:], po[-2:]) == ("BP", "SO", "PO")


# ── Missing-field fallbacks ───────────────────────────────────────────


@pytest.mark.parametrize(
    "so,customer,owner,expected",
    [
        (None, "Acme", "Sam", "— - Acme - Sam - BP"),  # no SO# yet (fresh draft)
        ("TSO-1", None, "Sam", "TSO-1 - — - Sam - BP"),  # customer site deleted
        ("TSO-1", "Acme", None, "TSO-1 - Acme - — - BP"),  # owner unset
        ("  ", "  ", "  ", "— - — - — - BP"),  # all blank/whitespace
    ],
)
def test_missing_fields_collapse_to_em_dash(so, customer, owner, expected):
    assert (
        build_card_title(sales_order_number=so, customer_name=customer, owner_name=owner, kind=CARD_KIND_BUY_PLAN)
        == expected
    )


def test_unknown_kind_raises():
    """An unrecognised card kind is a wiring bug — raise, don't render an untyped
    title."""
    with pytest.raises(ValueError, match="Unknown card kind"):
        build_card_title(sales_order_number="X", customer_name="Y", owner_name="Z", kind="XX")


# ── summarize_top_flag: the actual issue at first glance (Part 4) ──────


def test_top_flag_none_when_empty():
    assert summarize_top_flag(None) is None
    assert summarize_top_flag([]) is None


def test_top_flag_returns_verbatim_reason():
    """The reason string is the verbatim message the flag system recorded."""
    flags = [{"type": "low_margin", "severity": "warning", "message": "Margin 8.50% below 15% threshold"}]
    top = summarize_top_flag(flags)
    assert top == {"severity": "warning", "message": "Margin 8.50% below 15% threshold"}


def test_top_flag_picks_worst_severity():
    """Critical beats warning beats info, regardless of list order."""
    flags = [
        {"severity": "info", "message": "cheaper offer exists"},
        {"severity": "warning", "message": "stale offer"},
        {"severity": "critical", "message": "No buyer assigned for line (reason: unknown)"},
    ]
    top = summarize_top_flag(flags)
    assert top["severity"] == "critical"
    assert top["message"] == "No buyer assigned for line (reason: unknown)"


def test_top_flag_ties_keep_first():
    """Among equal-severity flags, the first one wins (stable)."""
    flags = [
        {"severity": "warning", "message": "first warning"},
        {"severity": "warning", "message": "second warning"},
    ]
    assert summarize_top_flag(flags)["message"] == "first warning"

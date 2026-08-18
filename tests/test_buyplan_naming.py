"""test_buyplan_naming.py — the shared deal-card title + top-flag-reason helpers.

Covers:
- build_card_title assembles "{SO#} - {Customer} - {Owner} - {Type}" for each kind
- BP/SO/PO suffix is appended verbatim and per-kind
- Missing SO# / customer / owner each collapse to an em dash (never ragged)
- Unknown kind raises ValueError (loud wiring-mistake signal)

Depends on: app/services/buyplan_naming.
"""

from __future__ import annotations

import pytest

from app.services.buyplan_naming import (
    CARD_KIND_BUY_PLAN,
    CARD_KIND_PO,
    CARD_KIND_SALES_ORDER,
    build_card_title,
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

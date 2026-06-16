"""Tests for app/constants.py — verify StrEnum values match expected strings.

Called by: pytest
Depends on: app.constants
"""

import pytest

from app.constants import (
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
    UserRole,
)


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (RequisitionStatus.DRAFT, "draft"),
        (RequisitionStatus.ACTIVE, "active"),
        (RequisitionStatus.SOURCING, "sourcing"),
        (RequisitionStatus.OFFERS, "offers"),
        (RequisitionStatus.QUOTING, "quoting"),
        (RequisitionStatus.QUOTED, "quoted"),
        (RequisitionStatus.REOPENED, "reopened"),
        (RequisitionStatus.WON, "won"),
        (RequisitionStatus.LOST, "lost"),
        (RequisitionStatus.ARCHIVED, "archived"),
    ],
)
def test_requisition_status_values(member, value):
    assert member == value


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (OfferStatus.ACTIVE, "active"),
        (OfferStatus.REJECTED, "rejected"),
        (OfferStatus.SOLD, "sold"),
        (OfferStatus.WON, "won"),
    ],
)
def test_offer_status_values(member, value):
    assert member == value


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (QuoteStatus.DRAFT, "draft"),
        (QuoteStatus.SENT, "sent"),
        (QuoteStatus.WON, "won"),
        (QuoteStatus.LOST, "lost"),
        (QuoteStatus.REVISED, "revised"),
    ],
)
def test_quote_status_values(member, value):
    assert member == value


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (UserRole.BUYER, "buyer"),
        (UserRole.SALES, "sales"),
        (UserRole.TRADER, "trader"),
        (UserRole.MANAGER, "manager"),
        (UserRole.ADMIN, "admin"),
    ],
)
def test_user_role_values(member, value):
    assert member == value


def test_sourcing_status_has_archived():
    assert SourcingStatus.ARCHIVED == "archived"
    assert "archived" in [s.value for s in SourcingStatus]


def test_enum_is_str():
    """StrEnum values are equal to plain strings."""
    assert RequisitionStatus.ACTIVE == "active"
    assert "active" == RequisitionStatus.ACTIVE
    assert RequisitionStatus.ACTIVE in {"active", "sourcing"}

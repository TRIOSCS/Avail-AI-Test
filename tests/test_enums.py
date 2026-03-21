"""Tests for app/constants.py — verify StrEnum values match expected strings.

Called by: pytest
Depends on: app.constants
"""

from app.constants import (
    OfferStatus,
    QuoteStatus,
    RequisitionStatus,
    SourcingStatus,
    UserRole,
)


def test_requisition_status_values():
    assert RequisitionStatus.DRAFT == "draft"
    assert RequisitionStatus.ACTIVE == "active"
    assert RequisitionStatus.SOURCING == "sourcing"
    assert RequisitionStatus.OFFERS == "offers"
    assert RequisitionStatus.QUOTING == "quoting"
    assert RequisitionStatus.QUOTED == "quoted"
    assert RequisitionStatus.REOPENED == "reopened"
    assert RequisitionStatus.WON == "won"
    assert RequisitionStatus.LOST == "lost"
    assert RequisitionStatus.ARCHIVED == "archived"


def test_offer_status_values():
    assert OfferStatus.ACTIVE == "active"
    assert OfferStatus.REJECTED == "rejected"
    assert OfferStatus.SOLD == "sold"
    assert OfferStatus.WON == "won"


def test_quote_status_values():
    assert QuoteStatus.DRAFT == "draft"
    assert QuoteStatus.SENT == "sent"
    assert QuoteStatus.WON == "won"
    assert QuoteStatus.LOST == "lost"
    assert QuoteStatus.REVISED == "revised"


def test_user_role_values():
    assert UserRole.BUYER == "buyer"
    assert UserRole.SALES == "sales"
    assert UserRole.TRADER == "trader"
    assert UserRole.MANAGER == "manager"
    assert UserRole.ADMIN == "admin"


def test_sourcing_status_has_archived():
    assert SourcingStatus.ARCHIVED == "archived"
    assert "archived" in [s.value for s in SourcingStatus]


def test_enum_is_str():
    """StrEnum values are equal to plain strings."""
    assert RequisitionStatus.ACTIVE == "active"
    assert "active" == RequisitionStatus.ACTIVE
    assert RequisitionStatus.ACTIVE in {"active", "sourcing"}

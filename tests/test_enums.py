"""Tests for app/enums.py — verify enum values match expected strings."""

from app.enums import OfferStatus, QuoteStatus, RequisitionStatus, UserRole


def test_requisition_status_values():
    assert RequisitionStatus.draft == "draft"
    assert RequisitionStatus.active == "active"
    assert RequisitionStatus.sourcing == "sourcing"
    assert RequisitionStatus.offers == "offers"
    assert RequisitionStatus.quoting == "quoting"
    assert RequisitionStatus.quoted == "quoted"
    assert RequisitionStatus.reopened == "reopened"
    assert RequisitionStatus.won == "won"
    assert RequisitionStatus.lost == "lost"
    assert RequisitionStatus.archived == "archived"


def test_offer_status_values():
    assert OfferStatus.active == "active"
    assert OfferStatus.rejected == "rejected"
    assert OfferStatus.sold == "sold"
    assert OfferStatus.won == "won"


def test_quote_status_values():
    assert QuoteStatus.draft == "draft"
    assert QuoteStatus.sent == "sent"
    assert QuoteStatus.won == "won"
    assert QuoteStatus.lost == "lost"
    assert QuoteStatus.revised == "revised"


def test_user_role_values():
    assert UserRole.buyer == "buyer"
    assert UserRole.sales == "sales"
    assert UserRole.trader == "trader"
    assert UserRole.manager == "manager"
    assert UserRole.admin == "admin"


def test_enum_is_str():
    """StrEnum values are equal to plain strings."""
    assert RequisitionStatus.active == "active"
    assert "active" == RequisitionStatus.active
    assert RequisitionStatus.active in {"active", "sourcing"}

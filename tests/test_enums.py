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
        (RequisitionStatus.OPEN, "open"),
        (RequisitionStatus.RFQS_SENT, "rfqs_sent"),
        (RequisitionStatus.OFFERS, "offers"),
        (RequisitionStatus.QUOTED, "quoted"),
        (RequisitionStatus.WON, "won"),
        (RequisitionStatus.LOST, "lost"),
        (RequisitionStatus.HOTLIST, "hotlist"),
        (RequisitionStatus.CANCELLED, "cancelled"),
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


def test_sourcing_status_has_hotlist_not_archived():
    """Migration 210: hotlist replaced archived; the Archive view is a lens."""
    assert SourcingStatus.HOTLIST == "hotlist"
    values = [s.value for s in SourcingStatus]
    assert "hotlist" in values
    assert "archived" not in values


def test_sourcing_status_nonmember_sets():
    """TERMINAL / MONITOR / ARCHIVE_VIEW are constants, not enum members."""
    assert SourcingStatus.TERMINAL == frozenset({"won", "lost"})
    assert SourcingStatus.MONITOR == frozenset({"hotlist"})
    assert SourcingStatus.ARCHIVE_VIEW == frozenset({"won", "lost", "hotlist"})
    members = {s.value for s in SourcingStatus}
    assert "TERMINAL" not in SourcingStatus.__members__
    assert SourcingStatus.ARCHIVE_VIEW <= members


def test_enum_is_str():
    """StrEnum values are equal to plain strings."""
    assert RequisitionStatus.OPEN == "open"
    assert "open" == RequisitionStatus.OPEN
    assert RequisitionStatus.OPEN in {"open", "rfqs_sent"}

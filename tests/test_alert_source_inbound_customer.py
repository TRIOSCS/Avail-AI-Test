"""Tests for InboundCustomerSource — the FYI 'inbound from a customer' alert.

Covers eligibility (inbound channel + Customer account_type + ownership + undismissed +
recency), seen-exclusion draining the count, and the COALESCE(occurred_at, created_at)
recency fallback for poll-logged rows.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.constants import AlertKind, Channel, Direction
from app.models.intelligence import ActivityLog
from app.services.alerts.base import record_seen
from app.services.alerts.sources.inbound_customer import InboundCustomerSource


@pytest.fixture()
def source() -> InboundCustomerSource:
    return InboundCustomerSource()


def _now() -> datetime:
    return datetime.now(UTC)


def _make_inbound(db, company, *, user=None, **overrides) -> ActivityLog:
    """Create an inbound email ActivityLog on `company`, committed."""
    fields = {
        "user_id": user.id if user else None,
        "activity_type": "email_received",
        "channel": Channel.EMAIL,
        "direction": Direction.INBOUND,
        "company_id": company.id,
        "subject": "Re: Pricing inquiry",
        "occurred_at": _now(),
        "created_at": _now(),
    }
    fields.update(overrides)
    activity = ActivityLog(**fields)
    db.add(activity)
    db.commit()
    db.refresh(activity)
    return activity


def _own_customer(db, company, owner):
    """Mark `company` as a Customer-type account owned by `owner`."""
    company.account_type = "Customer"
    company.account_owner_id = owner.id
    db.commit()


def test_inbound_email_on_owned_customer_counts(db_session, test_user, test_company, source):
    """An inbound email on a Customer-type company the user owns, recent +
    undismissed."""
    _own_customer(db_session, test_company, test_user)
    activity = _make_inbound(db_session, test_company, user=test_user)

    assert source.count_for_user(db_session, test_user) == 1

    items = source.new_items_for_user(db_session, test_user)
    assert len(items) == 1
    assert items[0].ref_id == activity.id
    assert items[0].anchor == f"company-{test_company.id}"


def test_seen_drains_count(db_session, test_user, test_company, source):
    """Recording the item as seen drops the FYI count to zero."""
    _own_customer(db_session, test_company, test_user)
    activity = _make_inbound(db_session, test_company, user=test_user)
    assert source.count_for_user(db_session, test_user) == 1

    record_seen(db_session, test_user, AlertKind.INBOUND_CUSTOMER, activity.id)

    assert source.count_for_user(db_session, test_user) == 0
    assert source.new_items_for_user(db_session, test_user) == []


def test_outbound_not_counted(db_session, test_user, test_company, source):
    """Outbound communications are not inbound-customer alerts."""
    _own_customer(db_session, test_company, test_user)
    _make_inbound(
        db_session,
        test_company,
        user=test_user,
        direction=Direction.OUTBOUND,
        activity_type="email_sent",
    )

    assert source.count_for_user(db_session, test_user) == 0


def test_non_customer_account_type_not_counted(db_session, test_user, test_company, source):
    """Inbound on a Prospect (non-Customer) account does not count."""
    test_company.account_type = "Prospect"
    test_company.account_owner_id = test_user.id
    db_session.commit()
    _make_inbound(db_session, test_company, user=test_user)

    assert source.count_for_user(db_session, test_user) == 0


def test_ownership_scopes_to_owner(db_session, test_user, sales_user, test_company, source):
    """Counts for the account owner, not for an unrelated user."""
    _own_customer(db_session, test_company, sales_user)
    _make_inbound(db_session, test_company, user=sales_user)

    assert source.count_for_user(db_session, sales_user) == 1
    assert source.count_for_user(db_session, test_user) == 0


def test_dismissed_not_counted(db_session, test_user, test_company, source):
    """A dismissed activity row does not count."""
    _own_customer(db_session, test_company, test_user)
    _make_inbound(db_session, test_company, user=test_user, dismissed_at=_now())

    assert source.count_for_user(db_session, test_user) == 0


def test_old_row_below_recency_floor_not_counted(db_session, test_user, test_company, source):
    """A row whose new-timestamp is 60 days ago falls below the recency floor."""
    _own_customer(db_session, test_company, test_user)
    old = _now() - timedelta(days=60)
    _make_inbound(db_session, test_company, user=test_user, occurred_at=old, created_at=old)

    assert source.count_for_user(db_session, test_user) == 0


def test_null_occurred_at_uses_created_at_recency(db_session, test_user, test_company, source):
    """When occurred_at is NULL, the recent created_at carries the row (COALESCE
    path)."""
    _own_customer(db_session, test_company, test_user)
    _make_inbound(db_session, test_company, user=test_user, occurred_at=None, created_at=_now())

    assert source.count_for_user(db_session, test_user) == 1

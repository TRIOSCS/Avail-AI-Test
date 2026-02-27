"""Tests for auto_dedup_service.py — background AI vendor/company dedup.

Verifies auto-merge behavior, data preservation, and owner conflict handling.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from tests.conftest import engine

from app.models import Company, User, VendorCard
from app.services.auto_dedup_service import run_auto_dedup


@pytest.fixture()
def setup_user(db_session):
    user = User(
        email="test@trioscs.com", name="Test", role="buyer",
        azure_id="az-001", created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    return user


def test_empty_db_noop(db_session):
    """Empty database returns zero stats."""
    stats = run_auto_dedup(db_session)
    assert stats == {"vendors_merged": 0, "companies_merged": 0}


def test_no_duplicates_no_merges(db_session):
    """Distinct vendor/company names produce no merges."""
    db_session.add(VendorCard(normalized_name="alpha", display_name="Alpha", emails=[], phones=[]))
    db_session.add(VendorCard(normalized_name="beta", display_name="Beta", emails=[], phones=[]))
    db_session.add(Company(name="Gamma Corp", is_active=True))
    db_session.add(Company(name="Delta Inc", is_active=True))
    db_session.commit()

    stats = run_auto_dedup(db_session)
    assert stats["vendors_merged"] == 0
    assert stats["companies_merged"] == 0


def test_company_different_owners_skipped(db_session, setup_user):
    """Companies with different account owners are NOT auto-merged."""
    user2 = User(
        email="other@trioscs.com", name="Other", role="sales",
        azure_id="az-002", created_at=datetime.now(timezone.utc),
    )
    db_session.add(user2)
    db_session.flush()

    co1 = Company(name="Same Corp", is_active=True, account_owner_id=setup_user.id)
    co2 = Company(name="Same Corporation", is_active=True, account_owner_id=user2.id)
    db_session.add_all([co1, co2])
    db_session.commit()

    stats = run_auto_dedup(db_session)
    assert stats["companies_merged"] == 0
    # Both companies still exist
    assert db_session.get(Company, co1.id) is not None
    assert db_session.get(Company, co2.id) is not None

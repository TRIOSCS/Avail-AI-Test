"""Tests for customer_enrichment_batch — batch enrichment and email re-verification.

Covers: run_customer_enrichment_batch, run_email_reverification, early credit stop.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.crm import Company, CustomerSite, SiteContact
from tests.conftest import engine  # noqa: F401


@pytest.fixture
def _mock_settings():
    with patch("app.services.customer_enrichment_batch.settings") as mock_s:
        mock_s.customer_enrichment_enabled = True
        mock_s.customer_enrichment_cooldown_days = 90
        mock_s.customer_enrichment_contacts_per_account = 5
        yield mock_s


@pytest.fixture
def companies(db_session):
    cos = []
    for i, name in enumerate(["Alpha Corp", "Beta Inc"]):
        co = Company(name=name, domain=f"{name.split()[0].lower()}.com", is_active=True)
        db_session.add(co)
        db_session.flush()
        db_session.add(CustomerSite(company_id=co.id, site_name="HQ"))
        cos.append(co)
    db_session.commit()
    return cos


@pytest.mark.asyncio
async def test_batch_disabled(db_session):
    with patch("app.services.customer_enrichment_batch.settings") as mock_s:
        mock_s.customer_enrichment_enabled = False
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        result = await run_customer_enrichment_batch(db_session)
        assert result["status"] == "disabled"


@pytest.mark.asyncio
async def test_batch_no_gaps(db_session, _mock_settings):
    with patch("app.services.customer_enrichment_batch.get_enrichment_gaps", return_value=[]):
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        result = await run_customer_enrichment_batch(db_session)
        assert result["status"] == "no_gaps"


@pytest.mark.asyncio
async def test_batch_success(db_session, companies, _mock_settings):
    with (
        patch(
            "app.services.customer_enrichment_batch.enrich_customer_account",
            new_callable=AsyncMock,
            return_value={"ok": True, "contacts_added": 2},
        ),
        patch(
            "app.services.customer_enrichment_batch.get_enrichment_gaps",
            return_value=[
                {"company_id": companies[0].id, "account_owner_id": None},
                {"company_id": companies[1].id, "account_owner_id": None},
            ],
        ),
        patch(
            "app.services.customer_enrichment_batch.can_use_credits",
            return_value=True,
        ),
    ):
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        result = await run_customer_enrichment_batch(db_session)
        assert result["status"] == "completed"
        assert result["processed"] == 2
        assert result["enriched"] == 2


@pytest.mark.asyncio
async def test_batch_assigned_only(db_session, companies, _mock_settings):
    with (
        patch(
            "app.services.customer_enrichment_batch.get_enrichment_gaps",
            return_value=[
                {"company_id": companies[0].id, "account_owner_id": 1},
                {"company_id": companies[1].id, "account_owner_id": None},
            ],
        ),
        patch(
            "app.services.customer_enrichment_batch.enrich_customer_account",
            new_callable=AsyncMock,
            return_value={"ok": True, "contacts_added": 1},
        ),
        patch(
            "app.services.customer_enrichment_batch.can_use_credits",
            return_value=True,
        ),
    ):
        from app.services.customer_enrichment_batch import run_customer_enrichment_batch

        result = await run_customer_enrichment_batch(db_session, assigned_only=True)
        assert result["processed"] == 1  # Only assigned


@pytest.mark.asyncio
async def test_email_reverification(db_session, _mock_settings):
    co = Company(name="Reverify Corp", domain="reverify.com", is_active=True)
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ")
    db_session.add(site)
    db_session.flush()

    old_date = datetime.now(timezone.utc) - timedelta(days=100)
    contact = SiteContact(
        customer_site_id=site.id,
        full_name="Old Verified",
        email="old@reverify.com",
        email_verified=True,
        email_verified_at=old_date,
    )
    db_session.add(contact)
    db_session.commit()

    with (
        patch(
            "app.connectors.hunter_client.verify_email",
            new_callable=AsyncMock,
            return_value={"email": "old@reverify.com", "status": "invalid", "score": 10},
        ),
        patch(
            "app.services.credit_manager.can_use_credits",
            return_value=True,
        ),
        patch(
            "app.services.credit_manager.record_credit_usage",
        ),
    ):
        from app.services.customer_enrichment_batch import run_email_reverification

        result = await run_email_reverification(db_session)
        assert result["processed"] == 1
        assert result["invalidated"] == 1

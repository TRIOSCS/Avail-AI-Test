"""
tests/test_routers_enrichment.py -- Tests for routers/enrichment.py

Covers: queue CRUD (list, approve, reject, bulk-approve), jobs (backfill,
list, detail, cancel), on-demand enrichment (vendor, company), stats,
email backfill, M365 status, deep scan, and website scraping.

Called by: pytest
Depends on: app/routers/enrichment.py, conftest.py
"""

import unittest.mock
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    EnrichmentJob,
    EnrichmentQueue,
    User,
)
from app.rate_limit import limiter

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    """TestClient with admin auth overrides."""
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_admin():
        return admin_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_admin
    app.dependency_overrides[require_admin] = _override_admin

    limiter.reset()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def queue_item(db_session, test_vendor_card):
    """A pending enrichment queue item."""
    item = EnrichmentQueue(
        vendor_card_id=test_vendor_card.id,
        enrichment_type="company_info",
        field_name="industry",
        current_value=None,
        proposed_value="Semiconductors",
        confidence=0.85,
        source="clearbit",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


@pytest.fixture()
def enrichment_job(db_session, admin_user):
    """A completed enrichment job."""
    job = EnrichmentJob(
        job_type="backfill",
        status="completed",
        total_items=100,
        processed_items=100,
        enriched_items=42,
        error_count=3,
        scope={"entity_types": ["vendor"]},
        started_by_id=admin_user.id,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


# ── Queue: list ──────────────────────────────────────────────────────


def test_queue_list_empty(client):
    """No pending items -> empty list."""
    resp = client.get("/api/enrichment/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_queue_list_with_items(client, queue_item):
    """Returns pending enrichment queue items."""
    resp = client.get("/api/enrichment/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(i["id"] == queue_item.id for i in data["items"])


def test_queue_filter_by_entity_type(client, queue_item):
    """Filter queue by entity_type=vendor."""
    resp = client.get("/api/enrichment/queue?entity_type=vendor")
    assert resp.status_code == 200
    data = resp.json()
    assert all(i["entity_type"] == "vendor" for i in data["items"])


# ── Queue: approve ───────────────────────────────────────────────────


@patch("app.services.deep_enrichment_service.apply_queue_item", return_value=True)
def test_queue_approve(mock_apply, client, queue_item):
    """Approve item -> applied to vendor."""
    resp = client.post(f"/api/enrichment/queue/{queue_item.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


def test_queue_approve_not_found(client):
    """Invalid queue item -> 404."""
    resp = client.post("/api/enrichment/queue/99999/approve")
    assert resp.status_code == 404


# ── Queue: reject ────────────────────────────────────────────────────


def test_queue_reject(client, queue_item):
    """Reject item -> marked rejected."""
    resp = client.post(f"/api/enrichment/queue/{queue_item.id}/reject")
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


def test_queue_reject_not_found(client):
    """Invalid queue item -> 404."""
    resp = client.post("/api/enrichment/queue/99999/reject")
    assert resp.status_code == 404


# ── Queue: bulk approve ──────────────────────────────────────────────


@patch("app.services.deep_enrichment_service.apply_queue_item", return_value=True)
def test_queue_bulk_approve(mock_apply, client, queue_item):
    """Approve multiple items."""
    resp = client.post("/api/enrichment/queue/bulk-approve", json={"ids": [queue_item.id]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["approved"] >= 1


# ── Jobs: backfill ───────────────────────────────────────────────────


@patch("app.services.deep_enrichment_service.run_backfill_job", new_callable=AsyncMock, return_value=1)
def test_backfill_start(mock_run, admin_client):
    """Admin starts backfill job -> 200."""
    resp = admin_client.post("/api/enrichment/backfill", json={
        "entity_types": ["vendor"], "max_items": 100,
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"
    assert resp.json()["job_id"] == 1


def test_backfill_non_admin(client):
    """Non-admin -> denied (require_admin calls require_user directly, not via DI)."""
    resp = client.post("/api/enrichment/backfill", json={
        "entity_types": ["vendor"], "max_items": 100,
    })
    assert resp.status_code in (401, 403)


@patch("app.services.deep_enrichment_service.run_backfill_job", new_callable=AsyncMock, return_value=2)
def test_backfill_already_running(mock_run, admin_client, db_session, admin_user):
    """Concurrent backfill -> 409."""
    running_job = EnrichmentJob(
        job_type="backfill", status="running",
        total_items=500, processed_items=100,
        started_by_id=admin_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(running_job)
    db_session.commit()

    resp = admin_client.post("/api/enrichment/backfill", json={
        "entity_types": ["vendor"], "max_items": 100,
    })
    assert resp.status_code == 409


# ── Jobs: list ───────────────────────────────────────────────────────


def test_jobs_list(client, enrichment_job):
    """Returns enrichment jobs."""
    resp = client.get("/api/enrichment/jobs")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["jobs"]) >= 1


# ── Jobs: detail ─────────────────────────────────────────────────────


def test_job_detail(client, enrichment_job):
    """Returns single job by ID."""
    resp = client.get(f"/api/enrichment/jobs/{enrichment_job.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == enrichment_job.id
    assert data["status"] == "completed"
    assert data["progress_pct"] == 100.0


def test_job_detail_not_found(client):
    """Invalid job ID -> 404."""
    resp = client.get("/api/enrichment/jobs/99999")
    assert resp.status_code == 404


# ── Jobs: cancel ─────────────────────────────────────────────────────


def test_job_cancel(admin_client, db_session, admin_user):
    """Admin cancels running job."""
    job = EnrichmentJob(
        job_type="backfill", status="running",
        total_items=500, processed_items=100,
        started_by_id=admin_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    db_session.commit()

    resp = admin_client.post(f"/api/enrichment/jobs/{job.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


def test_job_cancel_not_running(admin_client, enrichment_job):
    """Cannot cancel completed job."""
    resp = admin_client.post(f"/api/enrichment/jobs/{enrichment_job.id}/cancel")
    assert resp.status_code == 400


# ── On-demand: vendor ────────────────────────────────────────────────


@patch("app.services.deep_enrichment_service.deep_enrich_vendor", new_callable=AsyncMock,
       return_value={"status": "enriched", "fields": 5})
def test_enrich_vendor_success(mock_enrich, client, test_vendor_card):
    """Trigger vendor enrichment -> 200."""
    resp = client.post(f"/api/enrichment/vendor/{test_vendor_card.id}")
    assert resp.status_code == 200


def test_enrich_vendor_not_found(client):
    """Invalid vendor -> 404."""
    resp = client.post("/api/enrichment/vendor/99999")
    assert resp.status_code == 404


@patch("app.services.deep_enrichment_service.deep_enrich_vendor", new_callable=AsyncMock,
       side_effect=RuntimeError("API error"))
def test_enrich_vendor_service_error(mock_enrich, client, test_vendor_card):
    """Service throws -> exception propagates (no try/except in endpoint)."""
    with pytest.raises(RuntimeError, match="API error"):
        client.post(f"/api/enrichment/vendor/{test_vendor_card.id}")


# ── On-demand: company ───────────────────────────────────────────────


@patch("app.services.deep_enrichment_service.deep_enrich_company", new_callable=AsyncMock,
       return_value={"status": "enriched", "fields": 3})
def test_enrich_company_success(mock_enrich, client, test_company):
    """Trigger company enrichment -> 200."""
    resp = client.post(f"/api/enrichment/company/{test_company.id}")
    assert resp.status_code == 200


def test_enrich_company_not_found(client):
    """Invalid company -> 404."""
    resp = client.post("/api/enrichment/company/99999")
    assert resp.status_code == 404


# ── Stats ────────────────────────────────────────────────────────────


def test_stats_returns_counts(client):
    """Returns enrichment statistics."""
    resp = client.get("/api/enrichment/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "queue_pending" in data
    assert "vendors_enriched" in data
    assert "companies_total" in data
    assert "active_jobs" in data


# ── Email backfill ───────────────────────────────────────────────────


def test_backfill_emails(admin_client):
    """Admin triggers email backfill."""
    resp = admin_client.post("/api/enrichment/backfill-emails")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_created" in data


def test_backfill_emails_non_admin(client):
    """Non-admin -> denied."""
    resp = client.post("/api/enrichment/backfill-emails")
    assert resp.status_code in (401, 403)


# ── M365 status ──────────────────────────────────────────────────────


def test_m365_status(admin_client, admin_user):
    """Returns M365 connection status."""
    resp = admin_client.get("/api/enrichment/m365-status")
    assert resp.status_code == 200
    data = resp.json()
    assert "users" in data
    assert any(u["email"] == admin_user.email for u in data["users"])


def test_m365_status_non_admin(client):
    """Non-admin -> denied."""
    resp = client.get("/api/enrichment/m365-status")
    assert resp.status_code in (401, 403)


# ── Deep email scan ──────────────────────────────────────────────────


@patch("app.connectors.email_mining.EmailMiner")
@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="mock-token")
def test_deep_email_scan(mock_token, mock_miner_cls, admin_client, admin_user, db_session):
    """Admin triggers deep scan for user."""
    admin_user.m365_connected = True
    admin_user.access_token = "test-token"
    db_session.commit()

    mock_miner = mock_miner_cls.return_value
    mock_miner.deep_scan_inbox = AsyncMock(return_value={
        "messages_scanned": 100,
        "per_domain": {},
    })

    resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert "messages_scanned" in data


def test_deep_scan_invalid_user(admin_client):
    """Invalid user_id -> 404."""
    resp = admin_client.post("/api/enrichment/deep-email-scan/99999")
    assert resp.status_code == 404


# ── Website scraping ─────────────────────────────────────────────────


@patch("app.services.website_scraper.scrape_vendor_websites", new_callable=AsyncMock,
       return_value={"scraped": 10, "contacts_found": 5})
def test_scrape_websites(mock_scrape, admin_client):
    """Admin triggers website scrape."""
    resp = admin_client.post("/api/enrichment/scrape-websites")
    assert resp.status_code == 200
    assert resp.json()["scraped"] == 10


def test_scrape_non_admin(client):
    """Non-admin -> denied."""
    resp = client.post("/api/enrichment/scrape-websites")
    assert resp.status_code in (401, 403)


# ── Additional coverage tests ─────────────────────────────────────────


def test_queue_filter_by_status_all(client, queue_item):
    """Filter queue by status=all returns all items."""
    resp = client.get("/api/enrichment/queue?status=all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


def test_queue_filter_by_entity_type_company(client, db_session, test_company):
    """Filter queue by entity_type=company."""
    item = EnrichmentQueue(
        company_id=test_company.id,
        enrichment_type="company_info",
        field_name="website",
        current_value=None,
        proposed_value="https://acme.com",
        confidence=0.90,
        source="clearbit",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()

    resp = client.get("/api/enrichment/queue?entity_type=company")
    assert resp.status_code == 200
    data = resp.json()
    assert all(i["entity_type"] == "company" for i in data["items"])


def test_queue_filter_by_source(client, queue_item):
    """Filter queue by source."""
    resp = client.get("/api/enrichment/queue?source=clearbit")
    assert resp.status_code == 200
    data = resp.json()
    assert all(i["source"] == "clearbit" for i in data["items"])


def test_queue_item_with_vendor_contact(client, db_session, test_vendor_contact):
    """Queue item with vendor_contact_id shows entity_type=contact."""
    item = EnrichmentQueue(
        vendor_contact_id=test_vendor_contact.id,
        enrichment_type="contact_info",
        field_name="title",
        current_value=None,
        proposed_value="VP Sales",
        confidence=0.75,
        source="apollo",
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()

    resp = client.get("/api/enrichment/queue?status=all")
    assert resp.status_code == 200
    items = resp.json()["items"]
    contact_items = [i for i in items if i["entity_type"] == "contact"]
    assert len(contact_items) >= 1


@patch("app.services.deep_enrichment_service.apply_queue_item", return_value=True)
def test_queue_approve_low_confidence(mock_apply, client, db_session, test_vendor_card):
    """Approve item with low_confidence status also works."""
    item = EnrichmentQueue(
        vendor_card_id=test_vendor_card.id,
        enrichment_type="company_info",
        field_name="industry",
        proposed_value="Tech",
        confidence=0.55,
        source="clearbit",
        status="low_confidence",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()

    resp = client.post(f"/api/enrichment/queue/{item.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


@patch("app.services.deep_enrichment_service.apply_queue_item", return_value=False)
def test_queue_approve_apply_fails(mock_apply, client, queue_item):
    """Apply returns False -> 500 error."""
    resp = client.post(f"/api/enrichment/queue/{queue_item.id}/approve")
    assert resp.status_code == 500


def test_queue_approve_already_approved(client, db_session, queue_item):
    """Cannot approve item with non-pending status."""
    queue_item.status = "approved"
    db_session.commit()
    resp = client.post(f"/api/enrichment/queue/{queue_item.id}/approve")
    assert resp.status_code == 400


def test_queue_reject_already_rejected(client, db_session, queue_item):
    """Cannot reject item that's not pending."""
    queue_item.status = "rejected"
    db_session.commit()
    resp = client.post(f"/api/enrichment/queue/{queue_item.id}/reject")
    assert resp.status_code == 400


@patch("app.services.deep_enrichment_service.apply_queue_item", return_value=False)
def test_queue_bulk_approve_with_failures(mock_apply, client, queue_item):
    """Bulk approve counts failures."""
    resp = client.post("/api/enrichment/queue/bulk-approve", json={"ids": [queue_item.id]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["failed"] >= 1


@patch("app.services.deep_enrichment_service.apply_queue_item", return_value=True)
def test_queue_bulk_approve_skips_wrong_status(mock_apply, client, db_session, queue_item):
    """Bulk approve skips items with non-pending status."""
    queue_item.status = "approved"
    db_session.commit()
    resp = client.post("/api/enrichment/queue/bulk-approve", json={"ids": [queue_item.id]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["approved"] == 0


def test_job_detail_zero_total(client, db_session, admin_user):
    """Job with zero total items shows 0% progress."""
    job = EnrichmentJob(
        job_type="backfill", status="running",
        total_items=0, processed_items=0,
        started_by_id=admin_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    db_session.commit()

    resp = client.get(f"/api/enrichment/jobs/{job.id}")
    assert resp.status_code == 200
    assert resp.json()["progress_pct"] == 0.0


def test_job_detail_no_starter(client, db_session):
    """Job with no started_by_id shows null started_by."""
    job = EnrichmentJob(
        job_type="backfill", status="completed",
        total_items=10, processed_items=10,
        enriched_items=5,
        started_by_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    db_session.commit()

    resp = client.get(f"/api/enrichment/jobs/{job.id}")
    assert resp.status_code == 200
    assert resp.json()["started_by"] is None


def test_job_cancel_not_found(admin_client):
    """Cancel non-existent job -> 404."""
    resp = admin_client.post("/api/enrichment/jobs/99999/cancel")
    assert resp.status_code == 404


def test_deep_scan_user_no_m365(admin_client, db_session, admin_user):
    """Deep scan user without M365 connected -> 400."""
    from app.models import User
    target = User(
        email="nom365@trioscs.com", name="No M365", role="buyer",
        azure_id="az-nom365", m365_connected=False,
    )
    db_session.add(target)
    db_session.commit()

    resp = admin_client.post(f"/api/enrichment/deep-email-scan/{target.id}")
    assert resp.status_code == 400


@patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value=None)
def test_deep_scan_no_token(mock_token, admin_client, db_session, admin_user):
    """Deep scan with no valid token -> 400."""
    admin_user.m365_connected = True
    admin_user.access_token = None
    db_session.commit()

    resp = admin_client.post(f"/api/enrichment/deep-email-scan/{admin_user.id}")
    assert resp.status_code == 400


def test_enrich_vendor_with_force(client, test_vendor_card):
    """Trigger vendor enrichment with force=True."""
    with patch(
        "app.services.deep_enrichment_service.deep_enrich_vendor",
        new_callable=AsyncMock,
        return_value={"status": "enriched", "fields": 3},
    ) as mock_enrich:
        resp = client.post(
            f"/api/enrichment/vendor/{test_vendor_card.id}",
            json={"force": True},
        )
    assert resp.status_code == 200
    mock_enrich.assert_awaited_once_with(test_vendor_card.id, unittest.mock.ANY, force=True)


def test_enrich_company_with_force(client, test_company):
    """Trigger company enrichment with force=True."""
    with patch(
        "app.services.deep_enrichment_service.deep_enrich_company",
        new_callable=AsyncMock,
        return_value={"status": "enriched", "fields": 2},
    ) as mock_enrich:
        resp = client.post(
            f"/api/enrichment/company/{test_company.id}",
            json={"force": True},
        )
    assert resp.status_code == 200
    mock_enrich.assert_awaited_once_with(test_company.id, unittest.mock.ANY, force=True)


def test_backfill_emails_with_data(admin_client, db_session, test_vendor_card, admin_user):
    """Email backfill processes activity log, vendor card emails, and brokerbin sightings."""
    from app.models import ActivityLog, Sighting

    # Add an activity log entry with vendor_card_id and contact_email
    act = ActivityLog(
        user_id=admin_user.id,
        activity_type="email_sent",
        channel="email",
        vendor_card_id=test_vendor_card.id,
        contact_email="backfill@arrow.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(act)

    # Add email to vendor card for consolidation path
    test_vendor_card.emails = ["cardmail@arrow.com"]
    db_session.commit()

    resp = admin_client.post("/api/enrichment/backfill-emails")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_created"] >= 0
    assert "activity_log_created" in data
    assert "vendor_card_created" in data
    assert "brokerbin_created" in data


# ── Customer enrichment error return (line 678) ──────────────────────

from app.models import Company, CustomerSite
from app.models.crm import SiteContact


class TestCustomerEnrichEndpoint:
    @patch("app.services.customer_enrichment_service.enrich_customer_account",
           new_callable=AsyncMock, return_value={"error": "No API keys configured"})
    def test_enrich_customer_returns_error(self, mock_enrich, admin_client, db_session):
        """Enrichment returns error dict -> returned as-is (line 678)."""
        co = Company(name="Error Co", is_active=True)
        db_session.add(co)
        db_session.commit()

        resp = admin_client.post(
            f"/api/enrichment/customer/{co.id}",
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["error"] == "No API keys configured"

    @patch("app.services.customer_enrichment_service.enrich_customer_account",
           new_callable=AsyncMock, return_value={"ok": True, "contacts_added": 2})
    def test_enrich_customer_success(self, mock_enrich, admin_client, db_session):
        co = Company(name="Good Co", is_active=True)
        db_session.add(co)
        db_session.commit()

        resp = admin_client.post(f"/api/enrichment/customer/{co.id}", json={})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ── Customer backfill (lines 721-762) ────────────────────────────────


class TestCustomerBackfill:
    @patch("app.services.customer_enrichment_service.get_enrichment_gaps",
           return_value=[])
    def test_backfill_no_gaps(self, mock_gaps, admin_client):
        """No enrichment gaps -> early return (line 728)."""
        resp = admin_client.post("/api/enrichment/customer-backfill", json={})
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_gaps"

    @patch("app.services.customer_enrichment_service.enrich_customer_account",
           new_callable=AsyncMock, return_value={"ok": True, "contacts_added": 1})
    @patch("app.services.customer_enrichment_service.get_enrichment_gaps",
           return_value=[{"company_id": 1, "name": "Test", "account_owner_id": None}])
    def test_backfill_processes_gaps(self, mock_gaps, mock_enrich, admin_client, db_session):
        """Processes enrichment gaps and creates job (lines 730-762)."""
        resp = admin_client.post("/api/enrichment/customer-backfill", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert data["processed"] == 1

    @patch("app.services.customer_enrichment_service.enrich_customer_account",
           new_callable=AsyncMock, side_effect=RuntimeError("API down"))
    @patch("app.services.customer_enrichment_service.get_enrichment_gaps",
           return_value=[{"company_id": 1, "name": "Test", "account_owner_id": 1}])
    def test_backfill_exception_captured(self, mock_gaps, mock_enrich, admin_client, db_session):
        """Exception during enrichment is captured in errors list (lines 750-752)."""
        resp = admin_client.post("/api/enrichment/customer-backfill", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] >= 1


# ── Batch enrich exception (lines 820-822) ───────────────────────────


class TestBatchEnrichException:
    @patch("app.services.customer_enrichment_service.enrich_customer_account",
           new_callable=AsyncMock, side_effect=RuntimeError("API crash"))
    def test_batch_enrich_exception_captured(self, mock_enrich, admin_client, db_session):
        """Exception during batch enrich captured in errors (lines 820-822)."""
        co = Company(name="Batch Error Co", is_active=True)
        db_session.add(co)
        db_session.commit()

        resp = admin_client.post("/api/enrichment/batch", json={
            "company_ids": [co.id],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"] >= 1


# ── Enrichment status gaps (lines 877, 881) ──────────────────────────


class TestEnrichmentStatusGaps:
    def test_enrichment_status_unverified_contacts(self, client, db_session):
        """Contacts with no verified email/phone -> gaps reported (lines 877, 881)."""
        co = Company(name="GapCo", is_active=True)
        db_session.add(co)
        db_session.flush()
        site = CustomerSite(company_id=co.id, site_name="HQ")
        db_session.add(site)
        db_session.flush()
        contact = SiteContact(
            customer_site_id=site.id,
            full_name="Unverified Contact",
            email="unv@gapco.com",
            is_active=True,
            email_verified=False,
            phone_verified=False,
        )
        db_session.add(contact)
        db_session.commit()

        resp = client.get(f"/api/enrichment/status/{co.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert "no_verified_emails" in data["gaps"]
        assert "no_verified_phones" in data["gaps"]

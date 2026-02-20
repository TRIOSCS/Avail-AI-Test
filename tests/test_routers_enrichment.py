"""
tests/test_routers_enrichment.py -- Tests for routers/enrichment.py

Covers: queue CRUD (list, approve, reject, bulk-approve), jobs (backfill,
list, detail, cancel), on-demand enrichment (vendor, company), stats,
email backfill, M365 status, deep scan, and website scraping.

Called by: pytest
Depends on: app/routers/enrichment.py, conftest.py
"""

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

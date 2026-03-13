"""Tests for customer enrichment API endpoints.

Covers: POST customer/{id}, POST verify-email, GET credits,
        POST customer-backfill, GET customer-gaps.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("MVP_MODE", "true").lower() == "true",
    reason="Disabled in MVP mode",
)

from app.models.crm import Company, CustomerSite
from tests.conftest import engine  # noqa: F401


@pytest.fixture
def test_co(db_session):
    co = Company(name="API Test Corp", domain="apitest.com", is_active=True)
    db_session.add(co)
    db_session.flush()
    db_session.add(CustomerSite(company_id=co.id, site_name="HQ"))
    db_session.commit()
    db_session.refresh(co)
    return co


def test_customer_enrich_endpoint(client, db_session, test_co):
    with patch(
        "app.services.customer_enrichment_service.enrich_customer_account",
        new_callable=AsyncMock,
        return_value={"ok": True, "contacts_added": 3, "sources_used": ["lusha"], "status": "complete"},
    ):
        resp = client.post(f"/api/enrichment/customer/{test_co.id}", json={"force": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["contacts_added"] == 3


def test_customer_enrich_not_found(client):
    with patch(
        "app.services.customer_enrichment_service.enrich_customer_account",
        new_callable=AsyncMock,
    ):
        resp = client.post("/api/enrichment/customer/99999", json={})
        assert resp.status_code == 404


def test_verify_email_endpoint(client, db_session):
    with (
        patch(
            "app.connectors.hunter_client.verify_email",
            new_callable=AsyncMock,
            return_value={"email": "test@test.com", "status": "valid", "score": 95, "sources": 3},
        ),
        patch(
            "app.services.credit_manager.can_use_credits",
            return_value=True,
        ),
        patch(
            "app.services.credit_manager.record_credit_usage",
        ),
    ):
        resp = client.post("/api/enrichment/verify-email", json={"email": "test@test.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "valid"


def test_verify_email_credits_exhausted(client, db_session):
    with patch(
        "app.services.credit_manager.can_use_credits",
        return_value=False,
    ):
        resp = client.post("/api/enrichment/verify-email", json={"email": "test@test.com"})
        assert resp.status_code == 429


def test_credits_endpoint(client, db_session):
    with patch(
        "app.services.credit_manager.get_all_budgets",
        return_value=[
            {"provider": "lusha", "month": "2026-02", "used": 10, "limit": 300, "remaining": 290},
        ],
    ):
        resp = client.get("/api/enrichment/credits")
        assert resp.status_code == 200
        data = resp.json()
        assert "credits" in data
        assert data["credits"][0]["provider"] == "lusha"


def test_customer_gaps_endpoint(client, db_session, test_co):
    with patch(
        "app.services.customer_enrichment_service.get_enrichment_gaps",
        return_value=[
            {
                "company_id": test_co.id,
                "company_name": "API Test Corp",
                "domain": "apitest.com",
                "account_owner_id": None,
                "contacts_needed": 5,
                "current_status": None,
                "last_enriched": None,
            }
        ],
    ):
        resp = client.get("/api/enrichment/customer-gaps")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["gaps"]) == 1
        assert data["gaps"][0]["company_name"] == "API Test Corp"


# ── Batch endpoint tests ─────────────────────────────────────────


def test_batch_enrich_success(client, db_session, test_co):
    with patch(
        "app.services.customer_enrichment_service.enrich_customer_account",
        new_callable=AsyncMock,
        return_value={"ok": True, "contacts_added": 2, "sources_used": ["lusha"], "status": "complete"},
    ):
        resp = client.post("/api/enrichment/batch", json={"company_ids": [test_co.id]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["processed"] == 1
        assert data["enriched"] == 1
        assert data["job_id"] is not None


def test_batch_enrich_not_found(client, db_session):
    resp = client.post("/api/enrichment/batch", json={"company_ids": [99999]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["processed"] == 0
    assert data["errors"] == 1


def test_batch_enrich_max_50(client, db_session):
    resp = client.post("/api/enrichment/batch", json={"company_ids": list(range(1, 52))})
    assert resp.status_code == 422  # Pydantic validation: max 50


def test_batch_enrich_empty(client, db_session):
    resp = client.post("/api/enrichment/batch", json={"company_ids": []})
    assert resp.status_code == 422  # Pydantic validation: min 1


# ── Status endpoint tests ────────────────────────────────────────


def test_enrichment_status_with_contacts(client, db_session, test_co):
    from app.models.crm import SiteContact

    site = db_session.query(CustomerSite).filter_by(company_id=test_co.id).first()
    sc = SiteContact(
        customer_site_id=site.id,
        full_name="Jane Buyer",
        title="Procurement Manager",
        email="jane@apitest.com",
        email_verified=True,
        phone="+1-555-0100",
        phone_verified=True,
        contact_role="buyer",
    )
    db_session.add(sc)
    db_session.commit()

    resp = client.get(f"/api/enrichment/status/{test_co.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["company_id"] == test_co.id
    assert data["contact_count"] == 1
    assert data["contacts"][0]["full_name"] == "Jane Buyer"
    assert data["contacts"][0]["email_verified"] is True
    # Has buyer but missing technical and decision_maker
    assert "missing_technical" in data["gaps"]
    assert "missing_decision_maker" in data["gaps"]


def test_enrichment_status_no_contacts(client, db_session, test_co):
    resp = client.get(f"/api/enrichment/status/{test_co.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["contact_count"] == 0
    assert "missing_buyer" in data["gaps"]


def test_enrichment_status_not_found(client):
    resp = client.get("/api/enrichment/status/99999")
    assert resp.status_code == 404

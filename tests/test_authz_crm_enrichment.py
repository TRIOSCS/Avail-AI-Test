"""Regression tests: cross-tenant IDOR guard on app/routers/crm/enrichment.py.

POST /api/enrich/company/{id} runs paid external enrichment. A SALES (restricted)
non-owner must be blocked (403) before any enrichment spend; owner/manager pass the gate.

The endpoint calls _require_enrichment_provider() *before* the ownership gate, so we patch
the provider-credential lookup to a truthy value so the request reaches the gate under test.
For owner/manager happy paths we additionally stub enrich_entity /
apply_enrichment_to_company so no real network call fires — we only assert the gate lets
the request through (200).
"""

import pytest

from app.constants import UserRole
from app.models import Company


@pytest.fixture(autouse=True)
def _provider_configured(monkeypatch):
    """Make _require_enrichment_provider() pass so requests reach the ownership gate."""
    import app.routers.crm.enrichment as enrichment_router

    monkeypatch.setattr(enrichment_router, "get_credential_cached", lambda *a, **k: "TEST_KEY")


@pytest.fixture()
def foreign_company(db_session, admin_user):
    """A company owned by admin (so SALES test_user is a non-owner), with a domain
    set."""
    co = Company(name="Foreign Megacorp", account_owner_id=admin_user.id, domain="foreign.com", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


def test_enrich_company_blocks_non_owner_sales(client, db_session, test_user, foreign_company):
    test_user.role = UserRole.SALES
    db_session.commit()
    resp = client.post(f"/api/enrich/company/{foreign_company.id}")
    assert resp.status_code == 403


def test_enrich_company_allows_owning_sales(client, db_session, test_user, monkeypatch):
    """Owner SALES passes the gate (enrichment internals stubbed)."""
    import app.enrichment_service as enrichment_service

    async def _fake_enrich_entity(domain, name):
        return {"domain": domain}

    monkeypatch.setattr(enrichment_service, "enrich_entity", _fake_enrich_entity)
    monkeypatch.setattr(enrichment_service, "apply_enrichment_to_company", lambda company, e: [])

    test_user.role = UserRole.SALES
    co = Company(name="My Account", account_owner_id=test_user.id, domain="mine.com", is_active=True)
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)

    resp = client.post(f"/api/enrich/company/{co.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_enrich_company_allows_manager(client, db_session, test_user, foreign_company, monkeypatch):
    """Manager passes the gate regardless of ownership (enrichment internals
    stubbed)."""
    import app.enrichment_service as enrichment_service

    async def _fake_enrich_entity(domain, name):
        return {"domain": domain}

    monkeypatch.setattr(enrichment_service, "enrich_entity", _fake_enrich_entity)
    monkeypatch.setattr(enrichment_service, "apply_enrichment_to_company", lambda company, e: [])

    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.post(f"/api/enrich/company/{foreign_company.id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_enrich_company_missing_404(client, db_session, test_user):
    test_user.role = UserRole.MANAGER
    db_session.commit()
    resp = client.post("/api/enrich/company/999999")
    assert resp.status_code == 404

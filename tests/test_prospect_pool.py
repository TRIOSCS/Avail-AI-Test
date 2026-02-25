"""tests/test_prospect_pool.py — Prospect pool (Suggested tab) API tests."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, User


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def pool_companies(db_session: Session) -> list[Company]:
    """Create a mix of pool and owned companies."""
    companies = [
        Company(
            name="Pool Priority Co",
            domain="priority.com",
            industry="Industrial",
            phone="+1-555-0001",
            import_priority="priority",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        ),
        Company(
            name="Pool Standard Co",
            domain="standard.com",
            industry="Medical",
            phone="+1-555-0002",
            import_priority="standard",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        ),
        Company(
            name="Pool No Priority",
            domain="nopri.com",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        ),
        Company(
            name="Dismissed Co",
            domain="dismissed.com",
            import_priority="dismissed",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        ),
    ]
    db_session.add_all(companies)
    db_session.commit()
    for c in companies:
        db_session.refresh(c)
    return companies


@pytest.fixture()
def owned_company(db_session: Session, test_user: User) -> Company:
    """A company with an owner (not in pool)."""
    co = Company(
        name="Owned Corp",
        domain="owned.com",
        account_owner_id=test_user.id,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


# ── GET /api/prospects/pool ──────────────────────────────────────────


class TestPoolList:
    def test_list_returns_unowned_only(self, client, pool_companies, owned_company):
        resp = client.get("/api/prospects/pool")
        assert resp.status_code == 200
        data = resp.json()
        ids = [item["id"] for item in data["items"]]
        # Pool should include unowned, non-dismissed
        assert pool_companies[0].id in ids  # priority
        assert pool_companies[1].id in ids  # standard
        assert pool_companies[2].id in ids  # no priority
        # Should exclude dismissed and owned
        assert pool_companies[3].id not in ids  # dismissed
        assert owned_company.id not in ids

    def test_list_excludes_dismissed(self, client, pool_companies):
        resp = client.get("/api/prospects/pool")
        data = resp.json()
        ids = [item["id"] for item in data["items"]]
        assert pool_companies[3].id not in ids

    def test_list_pagination(self, client, pool_companies):
        resp = client.get("/api/prospects/pool?per_page=1&page=1")
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["per_page"] == 1
        assert data["page"] == 1
        assert data["total"] == 3  # 3 non-dismissed pool accounts

    def test_list_search_by_name(self, client, pool_companies):
        resp = client.get("/api/prospects/pool?search=Priority")
        data = resp.json()
        names = [item["name"] for item in data["items"]]
        assert "Pool Priority Co" in names
        assert "Pool Standard Co" not in names

    def test_list_search_by_domain(self, client, pool_companies):
        resp = client.get("/api/prospects/pool?search=standard.com")
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["domain"] == "standard.com"

    def test_list_filter_by_priority(self, client, pool_companies):
        resp = client.get("/api/prospects/pool?import_priority=priority")
        data = resp.json()
        assert all(item["import_priority"] == "priority" for item in data["items"])

    def test_list_filter_by_industry(self, client, pool_companies):
        resp = client.get("/api/prospects/pool?industry=Medical")
        data = resp.json()
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "Pool Standard Co"

    def test_list_has_pool_stats(self, client, pool_companies):
        resp = client.get("/api/prospects/pool")
        data = resp.json()
        stats = data["pool_stats"]
        assert stats["total_available"] == 3
        assert stats["priority_count"] == 1
        assert stats["standard_count"] == 1

    def test_list_sort_by_name(self, client, pool_companies):
        resp = client.get("/api/prospects/pool?sort_by=name")
        data = resp.json()
        names = [item["name"] for item in data["items"]]
        assert names == sorted(names)

    def test_list_empty_pool(self, client):
        resp = client.get("/api/prospects/pool")
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0


# ── GET /api/prospects/pool/stats ─────────────────────────────────────


class TestPoolStats:
    def test_stats(self, client, pool_companies):
        resp = client.get("/api/prospects/pool/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_available"] == 3
        assert data["priority_count"] == 1
        assert data["standard_count"] == 1

    def test_stats_empty(self, client):
        resp = client.get("/api/prospects/pool/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_available"] == 0


# ── GET /api/prospects/pool/{id} ──────────────────────────────────────


class TestPoolDetail:
    def test_detail(self, client, pool_companies):
        co = pool_companies[0]
        resp = client.get(f"/api/prospects/pool/{co.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Pool Priority Co"
        assert data["domain"] == "priority.com"

    def test_detail_404(self, client):
        resp = client.get("/api/prospects/pool/99999")
        assert resp.status_code == 404

    def test_detail_owned_returns_404(self, client, owned_company):
        resp = client.get(f"/api/prospects/pool/{owned_company.id}")
        assert resp.status_code == 404


# ── POST /api/prospects/pool/{id}/claim ───────────────────────────────


class TestPoolClaim:
    def test_claim_success(self, client, pool_companies, test_user, db_session):
        co = pool_companies[0]
        resp = client.post(f"/api/prospects/pool/{co.id}/claim")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "claimed"
        assert data["company_id"] == co.id

        db_session.refresh(co)
        assert co.account_owner_id == test_user.id
        assert co.import_priority is None

    def test_claim_removes_from_pool(self, client, pool_companies):
        co = pool_companies[0]
        client.post(f"/api/prospects/pool/{co.id}/claim")

        resp = client.get("/api/prospects/pool")
        ids = [item["id"] for item in resp.json()["items"]]
        assert co.id not in ids

    def test_claim_409_already_owned(self, client, owned_company):
        resp = client.post(f"/api/prospects/pool/{owned_company.id}/claim")
        assert resp.status_code == 409

    def test_claim_404(self, client):
        resp = client.post("/api/prospects/pool/99999/claim")
        assert resp.status_code == 404

    def test_double_claim_prevention(self, client, pool_companies):
        co = pool_companies[0]
        resp1 = client.post(f"/api/prospects/pool/{co.id}/claim")
        assert resp1.status_code == 200
        resp2 = client.post(f"/api/prospects/pool/{co.id}/claim")
        assert resp2.status_code == 409


# ── POST /api/prospects/pool/{id}/dismiss ─────────────────────────────


class TestPoolDismiss:
    def test_dismiss_success(self, client, pool_companies, db_session):
        co = pool_companies[0]
        resp = client.post(
            f"/api/prospects/pool/{co.id}/dismiss",
            json={"reason": "not_relevant"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "dismissed"

        db_session.refresh(co)
        assert co.import_priority == "dismissed"
        assert "not_relevant" in co.notes

    def test_dismiss_removes_from_pool(self, client, pool_companies):
        co = pool_companies[0]
        client.post(
            f"/api/prospects/pool/{co.id}/dismiss",
            json={"reason": "competitor"},
        )

        resp = client.get("/api/prospects/pool")
        ids = [item["id"] for item in resp.json()["items"]]
        assert co.id not in ids

    def test_dismiss_owned_returns_409(self, client, owned_company):
        resp = client.post(
            f"/api/prospects/pool/{owned_company.id}/dismiss",
            json={"reason": "not_relevant"},
        )
        assert resp.status_code == 409

    def test_dismiss_404(self, client):
        resp = client.post(
            "/api/prospects/pool/99999/dismiss",
            json={"reason": "not_relevant"},
        )
        assert resp.status_code == 404

    def test_dismiss_invalid_reason(self, client, pool_companies):
        co = pool_companies[0]
        resp = client.post(
            f"/api/prospects/pool/{co.id}/dismiss",
            json={"reason": "invalid_reason"},
        )
        assert resp.status_code == 422

    def test_dismiss_missing_reason(self, client, pool_companies):
        co = pool_companies[0]
        resp = client.post(f"/api/prospects/pool/{co.id}/dismiss", json={})
        assert resp.status_code == 422

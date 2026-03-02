"""
tests/test_admin_transfer.py — Tests for Mass Account Transfer endpoints

Covers GET /api/admin/transfer/preview and POST /api/admin/transfer/execute.

Called by: pytest
Depends on: conftest fixtures, app.routers.admin
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, User


@pytest.fixture()
def admin_client(db_session: Session, admin_user: User) -> TestClient:
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = lambda: admin_user
    app.dependency_overrides[require_admin] = lambda: admin_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def source_user(db_session: Session) -> User:
    u = User(
        email="source@trioscs.com",
        name="Source User",
        role="buyer",
        azure_id="src-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def target_user(db_session: Session) -> User:
    u = User(
        email="target@trioscs.com",
        name="Target User",
        role="buyer",
        azure_id="tgt-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def company_with_sites(db_session: Session, source_user: User) -> tuple:
    """Create a company with 3 sites owned by source_user."""
    co = Company(name="Transfer Corp", is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add(co)
    db_session.flush()
    sites = []
    for i in range(3):
        s = CustomerSite(
            company_id=co.id,
            site_name=f"Site {i + 1}",
            owner_id=source_user.id,
            city=f"City{i + 1}",
            state="TX",
            is_active=True,
        )
        db_session.add(s)
        sites.append(s)
    db_session.commit()
    for s in sites:
        db_session.refresh(s)
    return co, sites


# ── Preview Tests ─────────────────────────────────────────────────


def test_preview_happy_path(admin_client, source_user, company_with_sites):
    co, sites = company_with_sites
    resp = admin_client.get(f"/api/admin/transfer/preview?source_user_id={source_user.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source_user"]["id"] == source_user.id
    assert data["source_user"]["name"] == "Source User"
    assert data["site_count"] == 3
    assert len(data["sites"]) == 3
    names = {s["site_name"] for s in data["sites"]}
    assert names == {"Site 1", "Site 2", "Site 3"}


def test_preview_company_names_populated(admin_client, source_user, company_with_sites):
    co, sites = company_with_sites
    resp = admin_client.get(f"/api/admin/transfer/preview?source_user_id={source_user.id}")
    data = resp.json()
    for s in data["sites"]:
        assert s["company_name"] == "Transfer Corp"


def test_preview_no_sites(admin_client, target_user):
    resp = admin_client.get(f"/api/admin/transfer/preview?source_user_id={target_user.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["site_count"] == 0
    assert data["sites"] == []


def test_preview_source_not_found(admin_client):
    resp = admin_client.get("/api/admin/transfer/preview?source_user_id=99999")
    assert resp.status_code == 404


# ── Execute Tests ─────────────────────────────────────────────────


def test_execute_transfer_all(admin_client, db_session, source_user, target_user, company_with_sites):
    co, sites = company_with_sites
    site_ids = [s.id for s in sites]
    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": target_user.id,
            "site_ids": site_ids,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["transferred"] == 3
    assert data["skipped"] == 0
    assert data["skipped_ids"] == []
    assert data["source"]["id"] == source_user.id
    assert data["target"]["id"] == target_user.id

    # Verify DB state
    for sid in site_ids:
        s = db_session.get(CustomerSite, sid)
        assert s.owner_id == target_user.id


def test_execute_cherry_pick(admin_client, db_session, source_user, target_user, company_with_sites):
    co, sites = company_with_sites
    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": target_user.id,
            "site_ids": [sites[0].id],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["transferred"] == 1
    assert data["skipped"] == 0

    # Only first site transferred
    db_session.expire_all()
    assert db_session.get(CustomerSite, sites[0].id).owner_id == target_user.id
    assert db_session.get(CustomerSite, sites[1].id).owner_id == source_user.id


def test_execute_clears_ownership_cleared_at(admin_client, db_session, source_user, target_user, company_with_sites):
    co, sites = company_with_sites
    # Set ownership_cleared_at on first site
    sites[0].ownership_cleared_at = datetime.now(timezone.utc)
    db_session.commit()

    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": target_user.id,
            "site_ids": [sites[0].id],
        },
    )
    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.get(CustomerSite, sites[0].id).ownership_cleared_at is None


def test_execute_same_user_rejected(admin_client, source_user, company_with_sites):
    co, sites = company_with_sites
    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": source_user.id,
            "site_ids": [sites[0].id],
        },
    )
    assert resp.status_code == 400


def test_execute_source_not_found(admin_client, target_user):
    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": 99999,
            "target_user_id": target_user.id,
            "site_ids": [1],
        },
    )
    assert resp.status_code == 404


def test_execute_target_not_found(admin_client, source_user, company_with_sites):
    co, sites = company_with_sites
    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": 99999,
            "site_ids": [sites[0].id],
        },
    )
    assert resp.status_code == 404


def test_execute_site_not_owned_by_source_skipped(
    admin_client, db_session, source_user, target_user, company_with_sites
):
    co, sites = company_with_sites
    # Create a site owned by someone else
    other = User(
        email="other@trioscs.com",
        name="Other",
        role="buyer",
        azure_id="oth-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other)
    db_session.flush()
    other_site = CustomerSite(
        company_id=co.id,
        site_name="Other Site",
        owner_id=other.id,
    )
    db_session.add(other_site)
    db_session.commit()
    db_session.refresh(other_site)

    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": target_user.id,
            "site_ids": [sites[0].id, other_site.id],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["transferred"] == 1
    assert data["skipped"] == 1
    assert other_site.id in data["skipped_ids"]


def test_execute_empty_site_ids_rejected(admin_client, source_user, target_user):
    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": target_user.id,
            "site_ids": [],
        },
    )
    assert resp.status_code == 422


def test_execute_no_matching_sites(admin_client, source_user, target_user):
    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": target_user.id,
            "site_ids": [99999],
        },
    )
    assert resp.status_code == 400


def test_execute_partial_match(admin_client, db_session, source_user, target_user, company_with_sites):
    co, sites = company_with_sites
    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": target_user.id,
            "site_ids": [sites[0].id, sites[1].id, 99999],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["transferred"] == 2
    assert data["skipped"] == 1
    assert 99999 in data["skipped_ids"]


def test_execute_blocked_exceeds_cap(admin_client, db_session, source_user, target_user, company_with_sites):
    """Transfer blocked when it would push target user over 200-site cap."""
    from app.routers.v13_features import SITE_CAP_PER_USER

    co, sites = company_with_sites  # source_user owns 3 sites

    # Give target_user exactly SITE_CAP_PER_USER - 2 sites (room for 2 but not 3)
    for i in range(SITE_CAP_PER_USER - 2):
        db_session.add(
            CustomerSite(
                company_id=co.id,
                site_name=f"Target Existing {i}",
                owner_id=target_user.id,
                is_active=True,
            )
        )
    db_session.commit()

    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": target_user.id,
            "site_ids": [s.id for s in sites],  # 3 sites — would exceed cap
        },
    )
    assert resp.status_code == 409
    assert "cap" in resp.json()["error"].lower()


def test_execute_within_cap_succeeds(admin_client, db_session, source_user, target_user, company_with_sites):
    """Transfer succeeds when target user stays within cap."""
    co, sites = company_with_sites  # 3 sites

    resp = admin_client.post(
        "/api/admin/transfer/execute",
        json={
            "source_user_id": source_user.id,
            "target_user_id": target_user.id,
            "site_ids": [s.id for s in sites],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["transferred"] == 3

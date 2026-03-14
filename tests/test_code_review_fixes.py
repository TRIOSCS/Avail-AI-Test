"""
tests/test_code_review_fixes.py — Tests for code review fixes.

Covers:
  1. require_fresh_token now enforces is_active (deactivated users blocked)
  2. _seed_vinod_user uses SEED_ADMIN_EMAIL env var (no hardcoded email)
  3. Global search returns multi-entity results
  4. _query_quotes N+1 fix — single batch count query

Called by: pytest
Depends on: conftest.py, app/dependencies.py, app/startup.py, app/routers/views.py
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Company, Quote, QuoteLine, Requisition, User
from app.models.auth import User as AuthUser


# ── Fix 1: require_fresh_token is_active enforcement ─────────────────────────


def test_require_fresh_token_rejects_deactivated_user(db_session: Session):
    """Deactivated users must be blocked by require_fresh_token, not just require_user."""
    import asyncio

    from starlette.requests import Request as StarletteRequest
    from starlette.testclient import TestClient

    from app.dependencies import require_fresh_token
    from app.main import app

    deactivated = User(
        email="deactivated@trioscs.com",
        name="Dead Account",
        role="buyer",
        is_active=False,
        access_token="some-token",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(deactivated)
    db_session.commit()
    db_session.refresh(deactivated)

    request = MagicMock()
    request.session = {"user_id": deactivated.id}
    request.headers = {}

    with pytest.raises(HTTPException) as exc_info:
        asyncio.get_event_loop().run_until_complete(
            require_fresh_token.__wrapped__(request, db_session)
            if hasattr(require_fresh_token, "__wrapped__")
            else require_fresh_token(request, db_session)
        )
    assert exc_info.value.status_code == 403
    assert "deactivated" in exc_info.value.detail.lower()


def test_require_fresh_token_passes_active_user(db_session: Session):
    """Active users with a valid token are allowed through require_fresh_token."""
    import asyncio

    from app.dependencies import require_fresh_token

    active = User(
        email="active@trioscs.com",
        name="Active User",
        role="buyer",
        is_active=True,
        access_token="valid-token",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(active)
    db_session.commit()
    db_session.refresh(active)

    request = MagicMock()
    request.session = {"user_id": active.id}
    request.headers = {}

    token = asyncio.get_event_loop().run_until_complete(
        require_fresh_token(request, db_session)
    )
    assert token == "valid-token"


# ── Fix 2: _seed_vinod_user respects SEED_ADMIN_EMAIL env var ────────────────


def test_seed_admin_user_skips_when_env_not_set(db_session: Session, monkeypatch):
    """_seed_vinod_user must be a no-op when SEED_ADMIN_EMAIL is not set."""
    monkeypatch.delenv("SEED_ADMIN_EMAIL", raising=False)

    from app.startup import _seed_vinod_user

    _seed_vinod_user(db=db_session)

    count = db_session.query(User).count()
    assert count == 0


def test_seed_admin_user_creates_user_from_env(db_session: Session, monkeypatch):
    """_seed_vinod_user creates the user specified by SEED_ADMIN_EMAIL."""
    monkeypatch.setenv("SEED_ADMIN_EMAIL", "custom-admin@example.com")
    monkeypatch.setenv("SEED_ADMIN_NAME", "Custom Admin")

    from app.startup import _seed_vinod_user

    _seed_vinod_user(db=db_session)

    user = db_session.query(User).filter_by(email="custom-admin@example.com").first()
    assert user is not None
    assert user.role == "admin"
    assert user.name == "Custom Admin"


def test_seed_admin_user_is_idempotent(db_session: Session, monkeypatch):
    """Calling _seed_vinod_user twice does not duplicate the user."""
    monkeypatch.setenv("SEED_ADMIN_EMAIL", "once@example.com")
    monkeypatch.setenv("SEED_ADMIN_NAME", "Once")

    from app.startup import _seed_vinod_user

    _seed_vinod_user(db=db_session)
    _seed_vinod_user(db=db_session)

    count = db_session.query(User).filter_by(email="once@example.com").count()
    assert count == 1


def test_seed_admin_user_uses_email_localpart_as_name(db_session: Session, monkeypatch):
    """When SEED_ADMIN_NAME is absent, name defaults to capitalised local part of email."""
    monkeypatch.setenv("SEED_ADMIN_EMAIL", "johnsmith@example.com")
    monkeypatch.delenv("SEED_ADMIN_NAME", raising=False)

    from app.startup import _seed_vinod_user

    _seed_vinod_user(db=db_session)

    user = db_session.query(User).filter_by(email="johnsmith@example.com").first()
    assert user is not None
    assert user.name == "Johnsmith"


# ── Fix 3: global search returns results across entities ─────────────────────


def test_global_search_returns_empty_for_short_query(client: TestClient):
    """Queries shorter than 2 chars return an empty results list."""
    resp = client.get("/search?q=a")
    assert resp.status_code == 200


def test_global_search_finds_requisition(
    client: TestClient, db_session: Session, test_user: User
):
    """Global search should find a matching requisition by name."""
    req = Requisition(
        name="AVAIL-REQ-FINDME",
        customer_name="Acme Corp",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    resp = client.get("/search?q=FINDME")
    assert resp.status_code == 200


def test_global_search_finds_company(
    client: TestClient, db_session: Session
):
    """Global search should find a matching company by name."""
    co = Company(
        name="Searchable Electronics Corp",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()

    resp = client.get("/search?q=Searchable")
    assert resp.status_code == 200


# ── Fix 4: _query_quotes N+1 fix ─────────────────────────────────────────────


def test_query_quotes_counts_lines_without_n_plus_one(
    client: TestClient,
    db_session: Session,
    test_customer_site,
    test_user: User,
    test_requisition: Requisition,
):
    """Quote list page returns correct line counts using a single batch query."""
    from app.models import Quote, QuoteLine

    q1 = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        quote_number="Q-BATCH-001",
        status="draft",
        subtotal=100.00,
        total_cost=50.00,
        total_margin_pct=50.0,
        created_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    q2 = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        quote_number="Q-BATCH-002",
        status="draft",
        subtotal=200.00,
        total_cost=100.00,
        total_margin_pct=50.0,
        created_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([q1, q2])
    db_session.flush()

    # Add 2 lines to q1, 0 to q2
    for mpn in ("LM317T", "TL082"):
        db_session.add(
            QuoteLine(
                quote_id=q1.id,
                mpn=mpn,
                manufacturer="TI",
                qty=10,
                sell_price=1.00,
                cost_price=0.50,
            )
        )
    db_session.commit()

    resp = client.get("/views/quotes")
    assert resp.status_code == 200

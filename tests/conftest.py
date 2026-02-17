"""
conftest.py — Shared Test Fixtures for AVAIL AI

Provides an in-memory SQLite database, FastAPI TestClient with auth
overrides, and factory fixtures for core models (User, Requisition,
Company, VendorCard).

Business Rules:
- All tests run against isolated in-memory DB (no prod data risk)
- Auth is overridden so tests don't need M365 tokens
- Each test function gets a fresh DB session (auto-rollback)

Called by: all test files via pytest autodiscovery
Depends on: app.models (Base), app.database (get_db), app.dependencies
"""

import os
os.environ["TESTING"] = "1"  # Must be set before importing app modules

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base, Company, CustomerSite, Requisition, Requirement, User, VendorCard,
    ActivityLog, Quote, Offer, BuyPlan,
)

# ── In-memory SQLite engine ──────────────────────────────────────────
# SQLite can't handle PostgreSQL ARRAY columns — remap them to JSON.

TEST_DB_URL = "sqlite://"  # in-memory, fresh per session


def _patch_array_for_sqlite():
    """Register ARRAY → JSON type adapter so models work on SQLite."""
    from sqlalchemy import JSON
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


_patch_array_for_sqlite()

engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _enable_fk(dbapi_conn, _):
    """SQLite ignores FKs by default — turn them on."""
    dbapi_conn.execute("PRAGMA foreign_keys=ON")


# ── Fixtures ─────────────────────────────────────────────────────────

# Tables using PostgreSQL-only types (ARRAY) that SQLite can't handle.
# These are excluded from the test DB; tests needing them require PostgreSQL.
_PG_ONLY_TABLES = {"buyer_profiles"}


@pytest.fixture(autouse=True)
def db_session():
    """Create all tables, yield a session, then tear down."""
    _sqlite_safe = [
        t for name, t in Base.metadata.tables.items()
        if name not in _PG_ONLY_TABLES
    ]
    Base.metadata.create_all(bind=engine, tables=_sqlite_safe)
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine, tables=_sqlite_safe)


@pytest.fixture()
def test_user(db_session: Session) -> User:
    """A standard buyer user."""
    user = User(
        email="testbuyer@trioscs.com",
        name="Test Buyer",
        role="buyer",
        azure_id="test-azure-id-001",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def sales_user(db_session: Session) -> User:
    """A sales-role user (restricted access)."""
    user = User(
        email="testsales@trioscs.com",
        name="Test Sales",
        role="sales",
        azure_id="test-azure-id-002",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def test_company(db_session: Session) -> Company:
    """A sample customer company."""
    co = Company(
        name="Acme Electronics",
        website="https://acme-electronics.com",
        industry="Electronic Components",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def test_requisition(db_session: Session, test_user: User) -> Requisition:
    """A requisition with one requirement."""
    req = Requisition(
        name="REQ-TEST-001",
        customer_name="Acme Electronics",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()

    item = Requirement(
        requisition_id=req.id,
        primary_mpn="LM317T",
        target_qty=1000,
        target_price=0.50,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    return req


@pytest.fixture()
def test_vendor_card(db_session: Session) -> VendorCard:
    """A vendor card with basic data."""
    card = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        emails=["sales@arrow.com"],
        phones=["+1-555-0100"],
        sighting_count=42,
        website="https://arrow.com",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    return card


@pytest.fixture()
def client(db_session: Session, test_user: User) -> TestClient:
    """FastAPI TestClient with auth overridden to return test_user.

    Overrides get_db to use the test session and require_user to
    skip M365 auth entirely.
    """
    from app.database import get_db
    from app.dependencies import require_buyer, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    def _override_buyer():
        return test_user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_buyer] = _override_buyer

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def admin_user(db_session: Session) -> User:
    """An admin-role user for privileged operations."""
    user = User(
        email="admin@trioscs.com",
        name="Test Admin",
        role="admin",
        azure_id="test-azure-id-admin",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def test_activity(db_session: Session, test_user: User, test_company: Company) -> ActivityLog:
    """A sample activity log entry linked to a company."""
    activity = ActivityLog(
        user_id=test_user.id,
        activity_type="email_sent",
        channel="email",
        company_id=test_company.id,
        contact_email="vendor@example.com",
        contact_name="John Doe",
        subject="RFQ for LM317T",
        external_id="graph-msg-001",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(activity)
    db_session.commit()
    db_session.refresh(activity)
    return activity


@pytest.fixture()
def manager_user(db_session: Session) -> User:
    """A manager-role user for approval workflows."""
    user = User(
        email="manager@trioscs.com",
        name="Test Manager",
        role="manager",
        azure_id="test-azure-id-manager",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def trader_user(db_session: Session) -> User:
    """A trader-role user (restricted like sales)."""
    user = User(
        email="trader@trioscs.com",
        name="Test Trader",
        role="trader",
        azure_id="test-azure-id-trader",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def test_customer_site(db_session: Session, test_company: Company) -> CustomerSite:
    """A sample customer site linked to test_company."""
    site = CustomerSite(
        company_id=test_company.id,
        site_name="Acme HQ",
        contact_name="Jane Doe",
        contact_email="jane@acme-electronics.com",
    )
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return site


@pytest.fixture()
def test_quote(
    db_session: Session,
    test_requisition: Requisition,
    test_customer_site: CustomerSite,
    test_user: User,
) -> Quote:
    """A sent quote ready for buy plan submission."""
    q = Quote(
        requisition_id=test_requisition.id,
        customer_site_id=test_customer_site.id,
        quote_number="Q-2026-0001",
        status="sent",
        line_items=[],
        subtotal=1000.00,
        total_cost=500.00,
        total_margin_pct=50.00,
        created_by_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


@pytest.fixture()
def test_offer(
    db_session: Session,
    test_requisition: Requisition,
    test_user: User,
) -> Offer:
    """A vendor offer on the test requisition."""
    o = Offer(
        requisition_id=test_requisition.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=1000,
        unit_price=0.50,
        entered_by_id=test_user.id,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(o)
    db_session.commit()
    db_session.refresh(o)
    return o

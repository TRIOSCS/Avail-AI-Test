"""conftest.py — Shared Test Fixtures for AVAIL AI.

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

import nest_asyncio

# Many synchronous unit tests drive coroutines via
# ``asyncio.get_event_loop().run_until_complete(...)``. Under pytest-asyncio's auto mode
# an async test that ran earlier in the same (xdist) worker can leave the policy with no
# current event loop, so a later SYNC test's ``get_event_loop()`` raises "There is no
# current event loop" and the whole worker's remaining get_event_loop() tests fail by
# ordering. ``nest_asyncio.apply()`` keeps a usable loop available process-wide, which
# neutralizes that ordering hazard. Verified empirically: removing it turns ~60 such
# tests red under xdist. Kept deliberately (F13 reviewed — remove only after migrating
# every ``get_event_loop().run_until_complete`` call site to an async test / asyncio.run).
nest_asyncio.apply()

os.environ["TESTING"] = "1"  # Must be set before importing app modules
os.environ["RATE_LIMIT_ENABLED"] = "false"  # Disable rate limiting in tests
os.environ["DATABASE_URL"] = "sqlite://"  # Prevent any code from connecting to real PostgreSQL
os.environ["REDIS_URL"] = ""  # Prevent Redis connection attempts in tests
os.environ["CACHE_BACKEND"] = "none"  # Disable cache backend in tests
os.environ.setdefault("AGENT_API_KEY", "test-agent-key-secret")  # Agent session tests

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    ActivityLog,
    Base,
    Company,
    CustomerSite,
    MaterialCard,
    Offer,
    ProactiveOffer,
    Quote,
    Requirement,
    Requisition,
    User,
    VendorCard,
    VendorContact,
)

# ── In-memory SQLite engine ──────────────────────────────────────────
# SQLite can't handle PostgreSQL ARRAY columns — remap them to JSON.

TEST_DB_URL = "sqlite://"  # in-memory, fresh per session


def _patch_types_for_sqlite():
    """Register ARRAY → JSON, TSVECTOR → TEXT, JSONB → JSON type adapters so models work
    on SQLite."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"
    SQLiteTypeCompiler.visit_TSVECTOR = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"


_patch_types_for_sqlite()

engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)


@event.listens_for(engine, "connect")
def _enable_fk(dbapi_conn, _):
    """SQLite ignores FKs by default — turn them on."""
    dbapi_conn.execute("PRAGMA foreign_keys=ON")


# ── Fixtures ─────────────────────────────────────────────────────────

# Tables using PostgreSQL-only types (ARRAY) that SQLite can't handle.
# These are excluded from the test DB; tests needing them require PostgreSQL.
_PG_ONLY_TABLES = {"buyer_profiles"}

# Create tables once at import time — NOT per test.
_sqlite_safe = [t for name, t in Base.metadata.tables.items() if name not in _PG_ONLY_TABLES]
Base.metadata.create_all(bind=engine, tables=_sqlite_safe)

# Pre-compute delete order (respects FK dependencies via reversed create order).
#
# The companies <-> customer_sites <-> site_contacts trio is an FK cycle that
# ``sorted_tables`` cannot topologically order (it emits SAWarning 'unresolvable
# cycles' and drops those FK edges from the sort). Delete the cycle explicitly in
# child -> parent order FIRST, then everything else in reverse-create order. The
# per-table resilient cleanup below is what actually guarantees isolation, but a
# correct base order means the happy path never needs the retry.
_CYCLE_DELETE_FIRST = ("site_contacts", "customer_sites", "companies")
_tables_by_name = {t.name: t for t in Base.metadata.sorted_tables}
_delete_order_names = [n for n in _CYCLE_DELETE_FIRST if n in _tables_by_name] + [
    t.name for t in reversed(Base.metadata.sorted_tables) if t.name not in _CYCLE_DELETE_FIRST
]
_delete_stmts = [_tables_by_name[n].delete() for n in _delete_order_names if n not in _PG_ONLY_TABLES]


def _cleanup_all_rows() -> None:
    """Delete every row FK-safely, resilient to a single table's DELETE failing.

    Each DELETE runs inside its own SAVEPOINT so one failure — e.g. a future FK edge
    into the companies/customer_sites/site_contacts cycle that lacks a cascade — is
    isolated and rolled back on its own, instead of aborting the whole transaction and
    leaking EVERY table's rows into the next test (the multi-test cascade signature of
    the xdist flake clusters). Tables that fail the first pass are retried once after
    the rest are cleared (their FK parents/children are gone by then). A row that still
    won't delete is logged loudly rather than silently swallowed.
    """
    with engine.begin() as conn:
        failed = []
        for stmt in _delete_stmts:
            sp = conn.begin_nested()
            try:
                conn.execute(stmt)
                sp.commit()
            except Exception:
                sp.rollback()
                failed.append(stmt)
        for stmt in failed:
            sp = conn.begin_nested()
            try:
                conn.execute(stmt)
                sp.commit()
            except Exception:
                sp.rollback()
                logger.warning("Test cleanup could not delete {} — rows may leak", stmt.table.name)


@pytest.fixture(autouse=True)
def _clear_8x8_token_cache():
    """Clear the 8x8 module-level token cache before and after each test."""
    from app.services.eight_by_eight_service import _token_cache

    _token_cache.clear()
    yield
    _token_cache.clear()


@pytest.fixture(autouse=True)
def _clear_connector_token_cache():
    """Clear the cross-search OAuth token cache + per-key locks before and after each
    test.

    The DigiKey/eBay/Nexar bearer cache (`app.connectors.sources._token_cache`) is
    process-wide, so without this a token minted by one test would leak into the
    next. The paired `_token_locks` hold `asyncio.Lock`s bound to the event loop
    that first used them; under pytest-asyncio's per-test loops a reused lock would
    raise "bound to a different event loop", so both dicts are cleared each test.
    """
    from app.connectors.sources import _token_cache, _token_locks

    _token_cache.clear()
    _token_locks.clear()
    yield
    _token_cache.clear()
    _token_locks.clear()


@pytest.fixture(autouse=True)
def _reset_ai_gate_state():
    """Reset every search-worker AI-gate's cooldown + classification cache per test.

    Each worker gate (nc/ics/tbf) is a thin shim over one shared ``AIGate`` instance
    that carries a module-level ``_last_api_failure`` cooldown timestamp and an
    in-memory classification cache. A fail-open test drives the real API-failure path,
    which sets the cooldown to ``time.monotonic()``; the base gate then silently
    no-ops ``process_ai_gate`` for 300s. Under xdist that poisons every later gate
    test in the same worker (items stay 'pending', ``status == 'queued'`` asserts
    fail) — passes in isolation, flakes in the full suite. Reset both the module-level
    name and the underlying gate instance, on all three workers, before AND after each
    test so no test inherits a poisoned cooldown or a stale cache.
    """
    from app.services.ics_worker import ai_gate as ics_gate
    from app.services.nc_worker import ai_gate as nc_gate
    from app.services.tbf_worker import ai_gate as tbf_gate

    gate_modules = (nc_gate, ics_gate, tbf_gate)

    def _reset() -> None:
        for mod in gate_modules:
            mod._last_api_failure = 0.0
            mod._gate._last_api_failure = 0.0
            with mod._gate._cache_lock:
                mod._gate._classification_cache.clear()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _clear_known_html_hashes():
    """Clear the shared HTML-structure-hash registry before and after each test.

    ``search_worker_base.monitoring._known_html_hashes`` is one process-wide dict keyed
    by component ("NC"/"ICS"/"TBF"). The 'first hash never warns / changed structure
    warns' tests across three files depend on every consumer clearing it first; a test
    that seeds hashes without clearing (or asserts warning behavior after another file
    populated the same component set in the worker) flakes by ordering. Centralize the
    clear here so no per-file setup_method discipline is load-bearing.
    """
    from app.services.search_worker_base.monitoring import _known_html_hashes

    _known_html_hashes.clear()
    yield
    _known_html_hashes.clear()


@pytest.fixture(autouse=True)
def _restore_dependency_overrides():
    """Snapshot and restore ``app.main.app.dependency_overrides`` around every test.

    ``app.main.app`` is a process-wide singleton. Many tests install auth/db overrides
    inline (``app.dependency_overrides[dep] = ...``) and pop them AFTER their asserts,
    with no try/finally — so a failing assert (or a 30s timeout) leaks the override onto
    the shared app for the rest of that xdist worker, and every later test using
    ``unauthenticated_client`` (expects 401) or a real-authz client then fails for
    unrelated reasons. This is the amplification mechanism behind the multi-test
    user_mgmt/vendor/activities flake clusters. Snapshotting before and restoring the
    exact mapping after each test makes any leak impossible, worker-wide and for future
    tests, regardless of assertion outcome.
    """
    from app.main import app

    snapshot = dict(app.dependency_overrides)
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(snapshot)


@pytest.fixture(autouse=True)
def db_session():
    """Yield a session, then DELETE all rows (fast) instead of drop/create tables."""
    session = TestSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        # Delete all rows in FK-safe order — much faster than drop_all/create_all.
        # Per-table savepoints keep one failure from cascading (see _cleanup_all_rows).
        _cleanup_all_rows()
        session.close()


def force_card_category(db: Session, card: MaterialCard, raw_value: str) -> None:
    """Write an off-vocab/legacy category via Core UPDATE, bypassing the ORM guard.

    MaterialCard's ``@validates("category")`` rejects non-canonical assignment, but
    live legacy rows (mixed-case "DRAM", free-text vendor taxonomy) pre-date the
    guard — tests exercising that residue (faceted lower(trim()) bucketing, the
    startup residue warning, cleanup_known_bad) seed it here, exactly as a
    pre-guard writer would have. *card* must be flushed (needs an id).
    """
    from sqlalchemy import update as _sa_update

    db.execute(_sa_update(MaterialCard).where(MaterialCard.id == card.id).values(category=raw_value))
    db.expire(card, ["category"])


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

    Overrides get_db to use the test session and require_user to skip M365 auth
    entirely.
    """
    from app.database import get_db
    from app.dependencies import require_admin, require_buyer, require_fresh_token, require_user
    from app.main import app

    def _override_db():
        yield db_session

    def _override_user():
        return test_user

    def _override_buyer():
        return test_user

    async def _override_fresh_token():
        return "mock-token"

    overridden_deps = [get_db, require_user, require_admin, require_buyer, require_fresh_token]
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_buyer
    app.dependency_overrides[require_fresh_token] = _override_fresh_token

    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overridden_deps:
            app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def unauthenticated_client(db_session: Session) -> TestClient:
    """TestClient with DB override but NO user auth — for testing 401 paths."""
    from app.database import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def nonadmin_client(db_session: Session, test_user: User) -> TestClient:
    """TestClient authed as a non-admin buyer with require_admin LEFT REAL (for 403
    gating tests).

    A real session cookie is seeded so that require_admin (which calls require_user as a
    plain function, not via Depends) resolves the buyer through the genuine auth path
    and hits its role check — yielding a real 403, not a 401. The require_user Depends
    override covers the submission-path endpoints that stay open to any login.
    """
    import base64
    import json

    import itsdangerous

    from app.config import settings
    from app.database import get_db
    from app.dependencies import require_admin, require_user
    from app.main import app

    # require_admin must run for REAL so the gating tests get a genuine 403. Under xdist
    # another test can leak a global require_admin override onto the shared app; snapshot
    # and clear it so this fixture is order-independent, then restore on teardown.
    prior_admin_override = app.dependency_overrides.pop(require_admin, None)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: test_user
    signer = itsdangerous.TimestampSigner(str(settings.secret_key))
    session_cookie = signer.sign(base64.b64encode(json.dumps({"user_id": test_user.id}).encode())).decode()
    try:
        with TestClient(app) as c:
            c.cookies.set("session", session_cookie)
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_user, None)
        if prior_admin_override is not None:
            app.dependency_overrides[require_admin] = prior_admin_override


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
        quote_number="TEST-Q-2026-0001",
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
def test_buy_plan(
    db_session: Session,
    test_quote: Quote,
    test_requisition: Requisition,
):  # -> BuyPlan (local import; type annotation omitted to satisfy ruff F821)
    """A minimal BuyPlan linked to the test quote and requisition."""
    from app.models.buy_plan import BuyPlan

    bp = BuyPlan(
        quote_id=test_quote.id,
        requisition_id=test_requisition.id,
        status="draft",
        so_status="pending",
    )
    db_session.add(bp)
    db_session.commit()
    db_session.refresh(bp)
    return bp


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


@pytest.fixture()
def test_vendor_contact(db_session: Session, test_vendor_card: VendorCard) -> VendorContact:
    """A structured vendor contact linked to the test vendor card."""
    vc = VendorContact(
        vendor_card_id=test_vendor_card.id,
        full_name="John Sales",
        title="Sales Manager",
        email="john@arrow.com",
        phone="+1-555-0200",
        source="manual",
        is_verified=True,
        confidence=90,
    )
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


@pytest.fixture()
def test_material_card(db_session: Session) -> MaterialCard:
    """A material card for a common electronic component."""
    mc = MaterialCard(
        normalized_mpn="lm317t",
        display_mpn="LM317T",
        manufacturer="Texas Instruments",
        description="Adjustable Voltage Regulator",
        search_count=10,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(mc)
    db_session.commit()
    db_session.refresh(mc)
    return mc


@pytest.fixture()
def test_proactive_offer(
    db_session: Session,
    test_user: User,
    test_customer_site: CustomerSite,
) -> ProactiveOffer:
    """A sent proactive offer for conversion tests."""
    po = ProactiveOffer(
        customer_site_id=test_customer_site.id,
        salesperson_id=test_user.id,
        line_items=[
            {
                "mpn": "LM317T",
                "vendor_name": "Arrow Electronics",
                "qty": 1000,
                "cost": 0.50,
                "sell": 0.75,
            }
        ],
        recipient_emails=["jane@acme-electronics.com"],
        subject="Proactive Offer: LM317T",
        status="sent",
        total_sell=750.00,
        total_cost=500.00,
        sent_at=datetime.now(timezone.utc),
    )
    db_session.add(po)
    db_session.commit()
    db_session.refresh(po)
    return po

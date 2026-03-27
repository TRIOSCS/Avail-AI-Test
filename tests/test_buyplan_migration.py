"""test_buyplan_migration.py — Tests for V1→V3 buy plan migration.

Purpose: Verify that the migrate_v1_to_v3 function correctly converts
         legacy buy_plans rows into buy_plans_v3 + buy_plan_lines records.

Called by: pytest
Depends on: app.services.buyplan_migration, app.models.buy_plan, conftest fixtures
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.models.buy_plan import BuyPlan as BuyPlanV3
from app.models.buy_plan import BuyPlanLine
from app.services.buyplan_migration import migrate_v1_to_v3
from tests.conftest import engine

# ── V1 Table DDL ────────────────────────────────────────────────────

_CREATE_V1_TABLE = text("""
    CREATE TABLE IF NOT EXISTS buy_plans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        requisition_id INTEGER NOT NULL,
        quote_id INTEGER NOT NULL,
        status VARCHAR(50) DEFAULT 'draft',
        line_items JSON DEFAULT '[]',
        manager_notes TEXT,
        salesperson_notes TEXT,
        rejection_reason TEXT,
        sales_order_number VARCHAR(100),
        submitted_by_id INTEGER,
        approved_by_id INTEGER,
        submitted_at DATETIME,
        approved_at DATETIME,
        rejected_at DATETIME,
        completed_at DATETIME,
        completed_by_id INTEGER,
        cancelled_at DATETIME,
        cancelled_by_id INTEGER,
        cancellation_reason TEXT,
        approval_token VARCHAR(100),
        token_expires_at DATETIME,
        is_stock_sale BOOLEAN DEFAULT 0,
        total_cost NUMERIC(12,2),
        created_at DATETIME,
        migrated_to_v3_id INTEGER
    )
""")

_INSERT_V1_PLAN = text("""
    INSERT INTO buy_plans (
        requisition_id, quote_id, status, line_items,
        manager_notes, salesperson_notes, rejection_reason,
        sales_order_number, submitted_by_id, approved_by_id,
        submitted_at, approved_at, cancelled_at, cancelled_by_id,
        cancellation_reason, approval_token, token_expires_at,
        is_stock_sale, completed_at, created_at
    ) VALUES (
        :requisition_id, :quote_id, :status, :line_items,
        :manager_notes, :salesperson_notes, :rejection_reason,
        :sales_order_number, :submitted_by_id, :approved_by_id,
        :submitted_at, :approved_at, :cancelled_at, :cancelled_by_id,
        :cancellation_reason, :approval_token, :token_expires_at,
        :is_stock_sale, :completed_at, :created_at
    )
""")


# ── Helpers ──────────────────────────────────────────────────────────


def _create_prereqs(db, quote_id, requisition_id):
    """Create the requisition and quote records needed for FK constraints."""
    from app.models import Company, CustomerSite, Quote, Requisition, User

    # Ensure user exists
    user = db.execute(text("SELECT id FROM users LIMIT 1")).fetchone()
    if not user:
        u = User(
            email="migration-test@trioscs.com",
            name="Migration Test",
            role="buyer",
            azure_id="mig-test-001",
            created_at=datetime.now(timezone.utc),
        )
        db.add(u)
        db.flush()
        user_id = u.id
    else:
        user_id = user[0]

    # Ensure requisition exists
    existing_req = db.execute(text("SELECT id FROM requisitions WHERE id = :id"), {"id": requisition_id}).fetchone()
    if not existing_req:
        req = Requisition(
            id=requisition_id,
            name=f"REQ-MIG-{requisition_id}",
            customer_name="Migration Test Co",
            status="active",
            created_by=user_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(req)
        db.flush()

    # Ensure company + site for quote FK
    company = db.execute(text("SELECT id FROM companies LIMIT 1")).fetchone()
    if not company:
        co = Company(
            name="Migration Test Co",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(co)
        db.flush()
        company_id = co.id
    else:
        company_id = company[0]

    site = db.execute(text("SELECT id FROM customer_sites LIMIT 1")).fetchone()
    if not site:
        cs = CustomerSite(
            company_id=company_id,
            site_name="Test Site",
            contact_name="Test",
            contact_email="test@test.com",
        )
        db.add(cs)
        db.flush()
        site_id = cs.id
    else:
        site_id = site[0]

    # Ensure quote exists
    existing_quote = db.execute(text("SELECT id FROM quotes WHERE id = :id"), {"id": quote_id}).fetchone()
    if not existing_quote:
        q = Quote(
            id=quote_id,
            requisition_id=requisition_id,
            customer_site_id=site_id,
            quote_number=f"Q-MIG-{quote_id}",
            status="sent",
            line_items=[],
            created_by_id=user_id,
            created_at=datetime.now(timezone.utc),
        )
        db.add(q)
        db.flush()


def _insert_v1_plan(db, **overrides):
    """Insert a V1 buy plan row with sensible defaults.

    Returns the inserted row id.
    """
    defaults = {
        "requisition_id": 1,
        "quote_id": 1,
        "status": "draft",
        "line_items": "[]",
        "manager_notes": None,
        "salesperson_notes": None,
        "rejection_reason": None,
        "sales_order_number": None,
        "submitted_by_id": None,
        "approved_by_id": None,
        "submitted_at": None,
        "approved_at": None,
        "cancelled_at": None,
        "cancelled_by_id": None,
        "cancellation_reason": None,
        "approval_token": None,
        "token_expires_at": None,
        "is_stock_sale": False,
        "completed_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    defaults.update(overrides)
    # Serialize line_items if passed as a list
    if isinstance(defaults["line_items"], list):
        defaults["line_items"] = json.dumps(defaults["line_items"])

    # Ensure FK prerequisites exist
    _create_prereqs(db, defaults["quote_id"], defaults["requisition_id"])

    result = db.execute(_INSERT_V1_PLAN, defaults)
    db.flush()
    return result.lastrowid


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _v1_table(db_session):
    """Create V1 buy_plans table before each test, drop after."""
    db_session.execute(_CREATE_V1_TABLE)
    db_session.flush()
    yield
    # Use engine directly for teardown since session may be in a bad state
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS buy_plans"))


# ── Tests ────────────────────────────────────────────────────────────


def test_v1_plan_converts_to_v3(db_session):
    """A basic V1 plan should be migrated to a V3 plan with correct fields."""
    items = [{"mpn": "LM317T", "qty": 100, "cost_price": 0.50, "sell_price": 0.75}]
    v1_id = _insert_v1_plan(
        db_session,
        quote_id=10,
        requisition_id=20,
        status="approved",
        sales_order_number="SO-100",
        salesperson_notes="Rush order",
        approval_token="tok-abc",
        is_stock_sale=True,
        line_items=items,
    )

    result = migrate_v1_to_v3(db_session)
    db_session.flush()

    assert result["migrated"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == []

    v3 = db_session.query(BuyPlanV3).filter_by(migrated_from_v1=True).first()
    assert v3 is not None
    assert v3.quote_id == 10
    assert v3.requisition_id == 20
    assert v3.status == "active"  # approved → active
    assert v3.sales_order_number == "SO-100"
    assert v3.salesperson_notes == "Rush order"
    assert v3.approval_token == "tok-abc"
    assert v3.is_stock_sale is True

    # V1 back-reference should be set
    v1_row = db_session.execute(
        text("SELECT migrated_to_v3_id FROM buy_plans WHERE id = :id"),
        {"id": v1_id},
    ).fetchone()
    assert v1_row[0] == v3.id


def test_status_mapping(db_session):
    """Each V1 status maps to the correct V3 status."""
    expected_v3 = {
        "draft": "draft",
        "pending_approval": "pending",
        "approved": "active",
        "po_entered": "active",
        "po_confirmed": "active",
        "complete": "completed",
        "rejected": "draft",
        "cancelled": "cancelled",
    }

    v1_ids = {}
    for idx, v1_status in enumerate(expected_v3):
        v1_id = _insert_v1_plan(
            db_session,
            quote_id=100 + idx,
            requisition_id=200 + idx,
            status=v1_status,
            line_items=[],
        )
        v1_ids[v1_status] = v1_id

    result = migrate_v1_to_v3(db_session)
    db_session.flush()

    assert result["migrated"] == len(expected_v3)

    for v1_status, expected in expected_v3.items():
        v1_id = v1_ids[v1_status]
        v1_row = db_session.execute(
            text("SELECT migrated_to_v3_id FROM buy_plans WHERE id = :id"),
            {"id": v1_id},
        ).fetchone()
        v3 = db_session.get(BuyPlanV3, v1_row[0])
        assert v3 is not None
        assert v3.status == expected, f"V1 status '{v1_status}' should map to V3 '{expected}', got '{v3.status}'"


def test_line_items_to_lines(db_session):
    """JSON line_items should become BuyPlanLine rows with correct fields."""
    items = [
        {
            "mpn": "LM317T",
            "plan_qty": 500,
            "cost_price": 0.50,
            "sell_price": 0.75,
            "offer_id": None,
            "entered_by_id": None,
        },
        {
            "mpn": "NE555P",
            "qty": 200,
            "cost_price": 0.30,
            "sell_price": 0.45,
            "po_number": "PO-123",
            "po_verified": True,
        },
    ]

    _insert_v1_plan(
        db_session,
        quote_id=50,
        requisition_id=60,
        status="po_confirmed",
        line_items=items,
    )

    result = migrate_v1_to_v3(db_session)
    db_session.flush()

    assert result["migrated"] == 1

    lines = db_session.query(BuyPlanLine).order_by(BuyPlanLine.id).all()
    assert len(lines) == 2

    # First line: no PO → awaiting_po
    line1 = lines[0]
    assert line1.quantity == 500
    assert float(line1.unit_cost) == 0.50
    assert float(line1.unit_sell) == 0.75
    assert line1.status == "awaiting_po"
    assert line1.po_number is None

    # Second line: PO entered + verified → verified
    line2 = lines[1]
    assert line2.quantity == 200
    assert float(line2.unit_cost) == 0.30
    assert float(line2.unit_sell) == 0.45
    assert line2.status == "verified"
    assert line2.po_number == "PO-123"


def test_migration_idempotent(db_session):
    """Running migration twice should not create duplicate V3 plans."""
    _insert_v1_plan(
        db_session,
        quote_id=70,
        requisition_id=80,
        status="draft",
        line_items=[{"mpn": "LM317T", "qty": 100, "cost_price": 1.00}],
    )

    result1 = migrate_v1_to_v3(db_session)
    db_session.flush()
    assert result1["migrated"] == 1
    assert result1["skipped"] == 0

    # Run again
    result2 = migrate_v1_to_v3(db_session)
    db_session.flush()
    assert result2["migrated"] == 0
    assert result2["skipped"] == 1

    # Should still be exactly one V3 plan
    v3_count = db_session.query(BuyPlanV3).filter_by(migrated_from_v1=True).count()
    assert v3_count == 1

    # And exactly one line
    line_count = db_session.query(BuyPlanLine).count()
    assert line_count == 1


def test_empty_migration(db_session):
    """No V1 plans should produce zero migrated, zero errors."""
    result = migrate_v1_to_v3(db_session)

    assert result["migrated"] == 0
    assert result["skipped"] == 0
    assert result["errors"] == []


def test_rejected_plan_preserves_rejection_note(db_session):
    """A rejected V1 plan should map to draft with rejection reason in
    approval_notes."""
    _insert_v1_plan(
        db_session,
        quote_id=90,
        requisition_id=100,
        status="rejected",
        rejection_reason="Price too high",
        manager_notes="Need better pricing",
        line_items=[],
    )

    migrate_v1_to_v3(db_session)
    db_session.flush()

    v3 = db_session.query(BuyPlanV3).filter_by(migrated_from_v1=True).first()
    assert v3.status == "draft"
    assert "[Rejected] Price too high" in v3.approval_notes
    assert "Need better pricing" in v3.approval_notes


def test_cancelled_lines_get_cancelled_status(db_session):
    """All lines of a cancelled plan should have cancelled status."""
    _insert_v1_plan(
        db_session,
        quote_id=110,
        requisition_id=120,
        status="cancelled",
        cancellation_reason="Customer cancelled order",
        line_items=[
            {"mpn": "LM317T", "qty": 100, "cost_price": 0.50, "po_number": "PO-1"},
            {"mpn": "NE555P", "qty": 200, "cost_price": 0.30},
        ],
    )

    migrate_v1_to_v3(db_session)
    db_session.flush()

    lines = db_session.query(BuyPlanLine).all()
    assert len(lines) == 2
    for line in lines:
        assert line.status == "cancelled"


def test_totals_computed_from_line_items(db_session):
    """V3 total_cost and total_revenue should be computed from line items."""
    items = [
        {"mpn": "A", "qty": 10, "cost_price": 1.00, "sell_price": 1.50},
        {"mpn": "B", "plan_qty": 20, "cost_price": 2.00, "sell_price": 3.00},
    ]
    _insert_v1_plan(
        db_session,
        quote_id=130,
        requisition_id=140,
        status="approved",
        line_items=items,
    )

    migrate_v1_to_v3(db_session)
    db_session.flush()

    v3 = db_session.query(BuyPlanV3).filter_by(migrated_from_v1=True).first()
    # total_cost = 10*1.00 + 20*2.00 = 50.00
    assert float(v3.total_cost) == 50.0
    # total_revenue = 10*1.50 + 20*3.00 = 75.00
    assert float(v3.total_revenue) == 75.0


def test_string_line_items_parsed(db_session):
    """line_items stored as a JSON string (not dict) should still parse."""
    items_json = json.dumps([{"mpn": "X", "qty": 5, "cost_price": 10.0}])
    _insert_v1_plan(
        db_session,
        quote_id=150,
        requisition_id=160,
        status="draft",
        line_items=items_json,
    )

    result = migrate_v1_to_v3(db_session)
    db_session.flush()

    assert result["migrated"] == 1
    lines = db_session.query(BuyPlanLine).all()
    assert len(lines) == 1
    assert lines[0].quantity == 5

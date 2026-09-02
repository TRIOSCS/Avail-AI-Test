"""Tests for app/services/gp_report_service.py — the Reports page GP rollup (Decision
M).

Every test asserts on real output (period bounds, bucket keys, Decimal sums), never a bare
status/None check. Money is seeded as Decimal and read back as Decimal on both dialects.

Called by: pytest
Depends on: tests.conftest (_buyplan_req, _buyplan_plan, requires_postgres, pg_session),
            app.services.gp_report_service
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.constants import BuyPlanStatus
from app.models.auth import User
from app.models.crm import Company
from app.services.gp_report_service import (
    BASES,
    BOOKED_STATUSES,
    DEFAULT_BASIS,
    EXCLUDED_STATUSES,
    REALIZED_STATUSES,
    coerce,
    gp_rollup,
    resolve_period,
)
from tests.conftest import _buyplan_plan, _buyplan_req, requires_postgres

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
JUL = datetime(2026, 7, 15, tzinfo=UTC)
AUG = datetime(2026, 8, 15, tzinfo=UTC)
SEP = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def _plan(db: Session, req, user: User, **overrides):
    """A booked (pending) plan submitted by *user* in Aug 2026, priced 1000/600 unless
    overridden."""
    fields = dict(
        status="pending",
        submitted_by_id=user.id,
        submitted_at=AUG,
        total_revenue=Decimal("1000.00"),
        total_cost=Decimal("600.00"),
    )
    fields.update(overrides)
    return _buyplan_plan(db, req, **fields)


def _roll(db: Session, user: User, **overrides):
    args = dict(user_id=user.id, scope="all", group_by="month", basis="booked", period="6m", now=NOW)
    args.update(overrides)
    return gp_rollup(db, **args)


# ── pure helpers ──────────────────────────────────────────────────────────


def test_resolve_period_bounds_utc():
    def bounds(key, now=NOW):
        p = resolve_period(key, now=now)
        return (p.start, p.end)

    assert bounds("this_month") == (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))
    assert bounds("last_month") == (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC))
    assert bounds("3m") == (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))
    six = resolve_period("6m", now=NOW)
    assert (six.start, six.end) == (datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))
    assert six.label == "Apr 2026 – Sep 2026 · UTC calendar months"
    assert bounds("12m") == (datetime(2025, 10, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))
    # January rollover
    jan = datetime(2026, 1, 15, tzinfo=UTC)
    assert bounds("last_month", now=jan) == (datetime(2025, 12, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert resolve_period("this_month", now=NOW).label == "Sep 2026 · UTC calendar month"
    assert resolve_period("this_month", now=NOW).key == "this_month"


def test_resolve_period_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_period("fortnight", now=NOW)


def test_coerce_falls_back():
    assert coerce("bogus", BASES, DEFAULT_BASIS) == "booked"
    assert coerce("realized", BASES, DEFAULT_BASIS) == "realized"
    assert coerce(None, BASES, DEFAULT_BASIS) == "booked"
    assert coerce("", BASES, DEFAULT_BASIS) == "booked"


def test_every_buyplan_status_is_classified():
    # The CheckConstraint at app/models/buy_plan.py:182-186 derives from the enum; a new
    # status must be classified here or the booked basis silently drops/keeps it.
    assert BOOKED_STATUSES | EXCLUDED_STATUSES == set(BuyPlanStatus)
    assert not (BOOKED_STATUSES & EXCLUDED_STATUSES)
    assert REALIZED_STATUSES <= BOOKED_STATUSES


# ── basis filters ─────────────────────────────────────────────────────────


def test_booked_basis_counts_submitted_statuses_only(db_session: Session, test_user: User):
    req = _buyplan_req(db_session, test_user)
    for st in ("pending", "active", "inbound", "halted", "completed"):
        _plan(db_session, req, test_user, status=st)
    _plan(db_session, req, test_user, status="draft", submitted_at=None)  # never submitted
    _plan(db_session, req, test_user, status="cancelled")  # cancelled keeps its timestamp
    # Rejected / withdrawn: back to DRAFT with submitted_at STILL SET (buyplan_approval.py:218/:253)
    _plan(db_session, req, test_user, status="draft")

    rep = _roll(db_session, test_user, period="last_month")
    assert rep.total.plan_count == 5
    assert [(r.key, r.plan_count) for r in rep.rows] == [("2026-08", 5)]


def test_realized_basis_completed_only_by_completed_at(db_session: Session, test_user: User):
    req = _buyplan_req(db_session, test_user)
    _plan(db_session, req, test_user, status="active", completed_at=None)
    _plan(db_session, req, test_user, status="completed", submitted_at=JUL, completed_at=AUG)
    # Backorder-reopen shape: ACTIVE again — must not be realized even with a stale timestamp
    _plan(db_session, req, test_user, status="active", submitted_at=JUL, completed_at=JUL)

    rep = _roll(db_session, test_user, basis="realized", period="3m")
    assert rep.total.plan_count == 1
    assert [r.key for r in rep.rows] == ["2026-08"]


# ── unpriced handling + money ─────────────────────────────────────────────


def test_unpriced_plans_counted_never_summed(db_session: Session, test_user: User):
    req = _buyplan_req(db_session, test_user)
    _plan(db_session, req, test_user)  # 1000 / 600
    _plan(db_session, req, test_user, total_revenue=None, total_cost=None)  # lite order type
    _plan(db_session, req, test_user, total_revenue=Decimal("500.00"), total_cost=None)  # half-priced

    rep = _roll(db_session, test_user, period="last_month")
    row = rep.rows[0]
    assert (row.plan_count, row.unpriced_count) == (3, 2)
    assert row.revenue == Decimal("1000.00")
    assert row.cost == Decimal("600.00")
    assert row.gross_profit == Decimal("400.00")
    assert (rep.total.plan_count, rep.total.unpriced_count) == (3, 2)
    assert rep.total.revenue == Decimal("1000.00")


def test_all_unpriced_bucket_is_none_not_zero(db_session: Session, test_user: User):
    req = _buyplan_req(db_session, test_user)
    _plan(db_session, req, test_user, total_revenue=None, total_cost=None)
    _plan(db_session, req, test_user, total_revenue=None, total_cost=Decimal("10.00"))

    rep = _roll(db_session, test_user, period="last_month")
    row = rep.rows[0]
    assert row.revenue is None
    assert row.cost is None
    assert row.gross_profit is None
    assert row.margin_pct is None
    assert row.plan_count == row.unpriced_count == 2
    assert rep.total.gross_profit is None


def test_gp_and_margin_are_decimal_and_match_queue_rule(db_session: Session, test_user: User):
    req = _buyplan_req(db_session, test_user)
    _plan(db_session, req, test_user, total_revenue=Decimal("1234.56"), total_cost=Decimal("1000.00"))
    # Zero-revenue plan in a DIFFERENT month so its bucket is isolated
    _plan(db_session, req, test_user, submitted_at=JUL, total_revenue=Decimal("0.00"), total_cost=Decimal("0.00"))

    rep = _roll(db_session, test_user, period="3m")
    by_key = {r.key: r for r in rep.rows}
    aug = by_key["2026-08"]
    assert aug.gross_profit == Decimal("234.56")
    for field in (aug.revenue, aug.cost, aug.gross_profit, aug.margin_pct):
        assert isinstance(field, Decimal)
    assert aug.margin_pct == Decimal("19.00")  # money.pct: HALF_UP to 0.01
    jul = by_key["2026-07"]
    assert jul.revenue == Decimal("0.00")
    assert jul.margin_pct is None  # never divides by zero


# ── group-bys ─────────────────────────────────────────────────────────────


def test_group_by_month_orders_newest_first(db_session: Session, test_user: User):
    req = _buyplan_req(db_session, test_user)
    _plan(db_session, req, test_user, submitted_at=JUL)
    _plan(db_session, req, test_user, submitted_at=AUG)
    _plan(db_session, req, test_user, submitted_at=AUG)
    _plan(db_session, req, test_user, submitted_at=SEP)

    rep = _roll(db_session, test_user, period="3m")
    assert [r.key for r in rep.rows] == ["2026-09", "2026-08", "2026-07"]
    assert [r.label for r in rep.rows] == ["Sep 2026", "Aug 2026", "Jul 2026"]
    assert [r.plan_count for r in rep.rows] == [1, 2, 1]


def test_group_by_rep_labels_and_no_submitter(db_session: Session, test_user: User):
    email_only = User(email="noname@trioscs.com", name=None, role="sales", created_at=datetime.now(UTC))
    db_session.add(email_only)
    db_session.commit()
    req = _buyplan_req(db_session, test_user)
    _plan(db_session, req, test_user)  # GP 400
    _plan(db_session, req, email_only, total_revenue=Decimal("300.00"), total_cost=Decimal("200.00"))  # GP 100
    _plan(db_session, req, test_user, submitted_by_id=None, total_revenue=None, total_cost=None)  # unpriced

    rep = _roll(db_session, test_user, group_by="rep", period="last_month")
    assert [r.label for r in rep.rows] == ["Test Buyer", "noname@trioscs.com", "(no submitter)"]
    assert [r.key for r in rep.rows] == [str(test_user.id), str(email_only.id), "none"]
    assert [r.gross_profit for r in rep.rows] == [Decimal("400.00"), Decimal("100.00"), None]


def test_group_by_customer_prefers_company_name(db_session: Session, test_user: User):
    co = Company(name="Globex Corp", tier="core", is_active=True)
    db_session.add(co)
    db_session.commit()
    req_company = _buyplan_req(db_session, test_user, customer="Ignored Free Text")
    req_company.company_id = co.id
    db_session.commit()
    req_a1 = _buyplan_req(db_session, test_user, customer="Acme Electronics")
    req_a2 = _buyplan_req(db_session, test_user, customer="Acme Electronics")
    req_blank = _buyplan_req(db_session, test_user, customer="")
    for req in (req_company, req_a1, req_a2, req_blank):
        _plan(db_session, req, test_user)

    rep = _roll(db_session, test_user, group_by="customer", period="last_month")
    assert {r.label: r.plan_count for r in rep.rows} == {"Globex Corp": 1, "Acme Electronics": 2, "(no customer)": 1}
    assert rep.rows[0].label == "Acme Electronics"  # highest GP (800) first
    assert all(r.key == r.label for r in rep.rows)


# ── scope, bounds, totals, guards ─────────────────────────────────────────


def test_scope_mine_filters_submitted_by_id(db_session: Session, test_user: User, manager_user: User):
    req = _buyplan_req(db_session, test_user)
    _plan(db_session, req, test_user)
    _plan(db_session, req, manager_user)

    assert _roll(db_session, test_user, scope="mine", period="last_month").total.plan_count == 1
    assert _roll(db_session, test_user, scope="all", period="last_month").total.plan_count == 2


def test_period_bounds_inclusive_start_exclusive_end(db_session: Session, test_user: User):
    req = _buyplan_req(db_session, test_user)
    _plan(db_session, req, test_user, submitted_at=datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC))
    _plan(db_session, req, test_user, submitted_at=datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC))

    rep = _roll(db_session, test_user, period="last_month")
    assert rep.total.plan_count == 1
    assert [r.key for r in rep.rows] == ["2026-08"]


def test_total_row_is_db_sum_of_rows(db_session: Session, test_user: User):
    req = _buyplan_req(db_session, test_user)
    _plan(db_session, req, test_user, submitted_at=JUL, total_revenue=Decimal("10.50"), total_cost=Decimal("1.25"))
    _plan(db_session, req, test_user, submitted_at=AUG, total_revenue=Decimal("20.25"), total_cost=Decimal("2.75"))
    _plan(db_session, req, test_user, submitted_at=SEP, total_revenue=None, total_cost=None)

    rep = _roll(db_session, test_user, period="3m")
    assert rep.total.key == "total"
    assert rep.total.label == "Total"
    assert rep.total.plan_count == sum(r.plan_count for r in rep.rows) == 3
    assert rep.total.unpriced_count == sum(r.unpriced_count for r in rep.rows) == 1
    assert rep.total.revenue == sum(r.revenue for r in rep.rows if r.revenue is not None) == Decimal("30.75")
    assert rep.total.gross_profit == Decimal("26.75")


def test_gp_rollup_rejects_out_of_vocabulary(db_session: Session, test_user: User):
    for bad in (
        {"basis": "invoiced"},
        {"group_by": "vendor"},
        {"period": "ytd"},
        {"scope": "team"},
    ):
        with pytest.raises(ValueError):
            _roll(db_session, test_user, **bad)


# ── PostgreSQL path (extract() + Numeric SUM + session TimeZone) ─────────


@requires_postgres
def test_month_and_customer_group_by_on_postgres(pg_session: Session):
    user = User(email="pg-rep@trioscs.com", name="PG Rep", role="buyer", created_at=datetime.now(UTC))
    pg_session.add(user)
    pg_session.flush()
    co = Company(name="PG Customer Inc", tier="core", is_active=True)
    pg_session.add(co)
    pg_session.flush()
    req = _buyplan_req(pg_session, user)
    req.company_id = co.id
    pg_session.commit()
    _plan(pg_session, req, user)  # 1000 / 600
    _plan(pg_session, req, user, total_revenue=Decimal("500.00"), total_cost=Decimal("300.00"))

    expected_label = {"month": "Aug 2026", "rep": "PG Rep", "customer": "PG Customer Inc"}
    for group_by, label in expected_label.items():
        rep = _roll(pg_session, user, group_by=group_by, period="last_month")
        assert len(rep.rows) == 1, group_by
        row = rep.rows[0]
        assert row.label == label
        assert row.plan_count == 2
        for field in (row.revenue, row.cost, row.gross_profit, rep.total.revenue):
            assert isinstance(field, Decimal), (group_by, field)
        assert row.gross_profit == Decimal("600.00")
        assert row.margin_pct == Decimal("40.00")
    assert _roll(pg_session, user, group_by="month", period="last_month").rows[0].key == "2026-08"
    # EXTRACT on timestamptz buckets in the session TimeZone — the month key above is only
    # right when that is UTC. A non-UTC answer here is a real finding (raise it), not noise.
    assert pg_session.execute(text("SHOW timezone")).scalar() in ("UTC", "Etc/UTC")

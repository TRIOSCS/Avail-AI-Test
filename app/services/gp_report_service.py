"""GP report service — gross-profit rollup over buy-plan headers (Reports page, Decision
M).

Pure aggregation of persisted columns (BuyPlan.total_revenue / total_cost, frozen at
approval); no new financial logic. All sums run in SQL over Numeric; Python only derives
GP$ = revenue − cost (one subtraction of two DB sums — the approvals/queue.py:343-346 rule)
and margin via money.pct on Decimals. Margin is NOT computed in SQL: on SQLite the compiled
division is float, on PG a NUMERIC cast; pct on the two DB Decimals is exact on both.

Basis semantics (status-driven, NOT "timestamp not null" — verified against the workflow):
- Reject (buyplan_approval.py:218) and withdraw (:253) return a plan to DRAFT and never clear
  submitted_at; cancel keeps it too. So "booked" = submitted_at over BOOKED_STATUSES only.
- Resubmit overwrites submitted_at (:98-99) → a plan books in its latest submit month.
- A completed plan reopened on a backorder is ACTIVE with completed_at = NULL
  (buyplan_lines.py:184-187) → realized GP can move backwards month over month.
- Lite order types (Stock Sale / Testing Service / Comps) are zero-line plans whose headers
  are NULL (buyplan_builder.py:305-306); a revenue-but-NULL-cost header is possible via the
  recompute. All are UNPRICED: counted in plan_count and unpriced_count, never summed.
- HALTED is booked (a paused submitted plan) — owner question (a).

Timezone: bounds and buckets are UTC calendar months (labeled). UTCDateTime stores
TIMESTAMP WITH TIME ZONE; PG EXTRACT on timestamptz uses the session TimeZone, which the
@requires_postgres test asserts is UTC.

Called by: app/routers/htmx/reports.py
Depends on: app.models.buy_plan.BuyPlan, app.models.sourcing.Requisition,
            app.models.crm.Company, app.models.auth.User, app.constants.BuyPlanStatus,
            app.services.buyplan_workflow.money (to_money, pct)
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, case, extract, func, select
from sqlalchemy.orm import Session

from ..constants import BuyPlanStatus
from ..models.auth import User
from ..models.buy_plan import BuyPlan
from ..models.crm import Company
from ..models.sourcing import Requisition
from .buyplan_workflow.money import pct, to_money

# ── vocabularies (the router coerces to these; the service raises on anything else) ──

BASES: tuple[str, ...] = ("booked", "realized")
DEFAULT_BASIS = "booked"  # owner question (a) — one-constant reversal
BOOKED_STATUSES = frozenset(
    {
        BuyPlanStatus.PENDING,
        BuyPlanStatus.ACTIVE,
        BuyPlanStatus.INBOUND,
        BuyPlanStatus.HALTED,
        BuyPlanStatus.COMPLETED,
    }
)
REALIZED_STATUSES = frozenset({BuyPlanStatus.COMPLETED})
EXCLUDED_STATUSES = frozenset({BuyPlanStatus.DRAFT, BuyPlanStatus.CANCELLED})
# Invariant (tested): BOOKED_STATUSES | EXCLUDED_STATUSES == set(BuyPlanStatus)

GROUP_BYS: tuple[str, ...] = ("month", "rep", "customer")
DEFAULT_GROUP_BY = "month"
PERIODS: tuple[str, ...] = ("this_month", "last_month", "3m", "6m", "12m")
DEFAULT_PERIOD = "6m"  # owner question (e)
SCOPES: tuple[str, ...] = ("mine", "all")
MANAGER_DEFAULT_SCOPE = "all"  # owner question (f) — one-constant reversal

BASIS_LABELS: dict[str, str] = {
    "booked": (
        "Booked — by plan submit date (AVAIL submitted_at, not an ERP date) · statuses pending, "
        "active, inbound, halted, completed · drafts (incl. rejected/withdrawn back to draft) and "
        "cancelled excluded · a resubmitted plan books in its latest submit month"
    ),
    "realized": (
        "Realized — by plan completion date (AVAIL completed_at, not an ERP date) · completed plans "
        "only · a completed plan reopened on a backorder leaves this view until it completes again"
    ),
}
BASIS_SHORT_LABELS: dict[str, str] = {"booked": "Booked", "realized": "Realized"}
GROUP_LABELS: dict[str, str] = {"month": "Month", "rep": "Rep (submitted by)", "customer": "Customer"}
GROUP_PILL_LABELS: dict[str, str] = {"month": "Month", "rep": "Rep", "customer": "Customer"}
PERIOD_LABELS: dict[str, str] = {
    "this_month": "This month",
    "last_month": "Last month",
    "3m": "3 months",
    "6m": "6 months",
    "12m": "12 months",
}
# No apostrophes here — Jinja autoescape would render them as &#39; in the caption.
SCOPE_LABELS: dict[str, str] = {"mine": "Mine (plans you submitted)", "all": "All (plans from every user)"}
NO_REP_LABEL = "(no submitter)"
NO_CUSTOMER_LABEL = "(no customer)"

# period key → (start offset, end offset) in whole months from m0 = the first instant of
# now's month. Half-open [start, end).
_PERIOD_OFFSETS: dict[str, tuple[int, int]] = {
    "this_month": (0, 1),
    "last_month": (-1, 0),
    "3m": (-2, 1),
    "6m": (-5, 1),
    "12m": (-11, 1),
}


@dataclass(frozen=True)
class Period:
    key: str
    start: datetime  # aware UTC, inclusive
    end: datetime  # aware UTC, exclusive
    label: str  # "Apr 2026 – Sep 2026 · UTC calendar months" / "Aug 2026 · UTC calendar month"


@dataclass(frozen=True)
class GpRow:
    key: str  # month "2026-08" | rep str(user_id) or "none" | customer label text | "total"
    label: str
    plan_count: int  # every plan matching basis + period + scope in this bucket
    unpriced_count: int  # plans with total_revenue IS NULL OR total_cost IS NULL
    revenue: Decimal | None  # SUM(total_revenue) over PRICED plans; None when no priced plan
    cost: Decimal | None
    gross_profit: Decimal | None  # revenue − cost; None when either is None
    margin_pct: Decimal | None  # pct(gross_profit, revenue) when revenue > 0 else None


@dataclass(frozen=True)
class GpReport:
    basis: str
    group_by: str
    scope: str
    period: Period
    rows: list[GpRow]
    total: GpRow  # key "total" — a SECOND ungrouped execution of the same WHERE


def coerce(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    """*value* if it is in *allowed*, else *default*.

    Never raises (stale bookmarks must not 400).
    """
    return value if value in allowed else default


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _month_label(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


def resolve_period(period: str, *, now: datetime | None = None) -> Period:
    """UTC calendar-month bounds for a period key.

    m0 = first instant of now's month. this_month [m0, m0+1mo) · last_month [m0-1mo, m0)
    · 3m [m0-2mo, m0+1mo) · 6m [m0-5mo, m0+1mo) · 12m [m0-11mo, m0+1mo). Integer
    year/month arithmetic, no dateutil. ValueError on an unknown key (the router coerces
    first).
    """
    if period not in _PERIOD_OFFSETS:
        raise ValueError(f"unknown period {period!r}; expected one of {PERIODS}")
    now = now or datetime.now(UTC)
    start_off, end_off = _PERIOD_OFFSETS[period]
    sy, sm = _add_months(now.year, now.month, start_off)
    ey, em = _add_months(now.year, now.month, end_off)
    ly, lm = _add_months(ey, em, -1)  # last month INSIDE the window
    if (sy, sm) == (ly, lm):
        label = f"{_month_label(sy, sm)} · UTC calendar month"
    else:
        label = f"{_month_label(sy, sm)} – {_month_label(ly, lm)} · UTC calendar months"
    return Period(
        key=period,
        start=datetime(sy, sm, 1, tzinfo=UTC),
        end=datetime(ey, em, 1, tzinfo=UTC),
        label=label,
    )


def _row(key: str, label: str, rec) -> GpRow:
    """Build one GpRow from an aggregate record; the ONLY place money is derived."""
    revenue = to_money(rec.revenue)
    cost = to_money(rec.cost)
    gross_profit = revenue - cost if revenue is not None and cost is not None else None
    margin_pct = pct(gross_profit, revenue) if gross_profit is not None and revenue > 0 else None
    return GpRow(
        key=key,
        label=label,
        plan_count=int(rec.plan_count or 0),
        unpriced_count=int(rec.unpriced_count or 0),
        revenue=revenue,
        cost=cost,
        gross_profit=gross_profit,
        margin_pct=margin_pct,
    )


def _gp_desc(row: GpRow):
    """Sort key: gross_profit DESC with None last, ties by label (ordering, not money math)."""
    gp = row.gross_profit if row.gross_profit is not None else Decimal(0)
    return (row.gross_profit is None, -gp, row.label.casefold())


def gp_rollup(
    db: Session,
    *,
    user_id: int,
    scope: str,
    group_by: str,
    basis: str,
    period: str,
    now: datetime | None = None,
) -> GpReport:
    """Two SELECTs (grouped + total) over buy_plans_v3; see the module docstring for
    semantics.

    ValueError for values outside BASES / GROUP_BYS / PERIODS / SCOPES (programming-
    error guard; the router coerces). Money arrives as Decimal on PG and on SQLite
    (SQLAlchemy's Numeric result processor); every sum still passes through
    money.to_money so a dialect quirk can never leak a float.
    """
    for name, value, allowed in (
        ("basis", basis, BASES),
        ("group_by", group_by, GROUP_BYS),
        ("period", period, PERIODS),
        ("scope", scope, SCOPES),
    ):
        if value not in allowed:
            raise ValueError(f"{name} {value!r} not in {allowed}")
    per = resolve_period(period, now=now)

    ts = BuyPlan.submitted_at if basis == "booked" else BuyPlan.completed_at
    statuses = BOOKED_STATUSES if basis == "booked" else REALIZED_STATUSES
    where = [
        BuyPlan.status.in_([s.value for s in statuses]),
        ts.isnot(None),
        ts >= per.start,
        ts < per.end,
    ]
    if scope == "mine":
        where.append(BuyPlan.submitted_by_id == user_id)  # same predicate as queue.py:300-301 _scoped

    priced = and_(BuyPlan.total_revenue.isnot(None), BuyPlan.total_cost.isnot(None))
    aggs = (
        func.count(BuyPlan.id).label("plan_count"),
        func.sum(case((priced, 0), else_=1)).label("unpriced_count"),
        func.sum(case((priced, BuyPlan.total_revenue))).label("revenue"),  # else NULL → SUM skips
        func.sum(case((priced, BuyPlan.total_cost))).label("cost"),
    )

    total = _row("total", "Total", db.execute(select(*aggs).where(*where)).one())

    if group_by == "month":
        y = extract("year", ts).label("y")
        m = extract("month", ts).label("m")
        stmt = select(y, m, *aggs).where(*where).group_by(y, m).order_by(y.desc(), m.desc())
        rows = [_row(f"{int(r.y):04d}-{int(r.m):02d}", _month_label(int(r.y), int(r.m)), r) for r in db.execute(stmt)]
    elif group_by == "rep":
        # SET NULL FK → outer join keeps the plan; all three key columns in GROUP BY (PG rule).
        stmt = (
            select(BuyPlan.submitted_by_id, User.name, User.email, *aggs)
            .select_from(BuyPlan)
            .outerjoin(User, User.id == BuyPlan.submitted_by_id)
            .where(*where)
            .group_by(BuyPlan.submitted_by_id, User.name, User.email)
        )
        rows = [
            _row(
                "none" if r.submitted_by_id is None else str(r.submitted_by_id),
                r.name or r.email or NO_REP_LABEL,
                r,
            )
            for r in db.execute(stmt)
        ]
        rows.sort(key=_gp_desc)
    else:  # customer
        # requisition_id is NOT NULL (inner join drops nothing); company is optional.
        label_expr = func.coalesce(Company.name, func.nullif(Requisition.customer_name, ""), NO_CUSTOMER_LABEL).label(
            "label"
        )
        stmt = (
            select(label_expr, *aggs)
            .select_from(BuyPlan)
            .join(Requisition, Requisition.id == BuyPlan.requisition_id)
            .outerjoin(Company, Company.id == Requisition.company_id)
            .where(*where)
            .group_by(label_expr)
        )
        rows = [_row(r.label, r.label, r) for r in db.execute(stmt)]
        rows.sort(key=_gp_desc)

    return GpReport(basis=basis, group_by=group_by, scope=scope, period=per, rows=rows, total=total)

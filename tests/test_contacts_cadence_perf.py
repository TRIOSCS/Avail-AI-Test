"""PERF-10 — the global customer-contacts list must filter + page the cadence_state
facet in SQL, not by loading the entire role-scoped contact set into Python.

Two guarantees are locked here:

1. Performance: with a cadence_state filter set, only the requested page (LIMIT rows) is
   materialized as ORM objects — the cost does NOT grow with the size of the filtered
   set (the pre-fix code did ``base.all()`` then sliced in Python).
2. Identical results: ``contact_cadence_predicate`` (the SQL cutoff) classifies rows
   byte-for-byte identically to the original ``cadence_state_of`` Python classifier,
   across NULL / boundary-day / future-dated / duplicate edge cases, and the paged ctx
   output matches the replicated pre-fix algorithm exactly (same ids, order, total).

Called by: pytest
Depends on: app.services.crm_service.customer_contacts_list_ctx / customer_contacts_query /
    contact_cadence_predicate / cadence_state_of, app.models.crm (Company/CustomerSite/
    SiteContact).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User
from app.services.crm_service import (
    CADENCE_RED_DAYS,
    CONTACT_CADENCE_DOTS,
    TIER_TARGET_DAYS,
    cadence_state_of,
    contact_cadence_predicate,
    customer_contacts_list_ctx,
    customer_contacts_query,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _company_site(db: Session) -> tuple[Company, CustomerSite]:
    co = Company(name="Cadence Co", is_active=True)
    db.add(co)
    db.flush()
    site = CustomerSite(company_id=co.id, site_name="HQ", is_active=True)
    db.add(site)
    db.flush()
    return co, site


def _contact(db, site_id, *, name, last_outbound=None, last_activity=None) -> SiteContact:
    c = SiteContact(
        customer_site_id=site_id,
        full_name=name,
        is_active=True,
        last_outbound_at=last_outbound,
        last_activity_at=last_activity,
    )
    db.add(c)
    return c


class _LoadCounter:
    """Counts SiteContact rows materialized into ORM objects (identity map must be
    clear)."""

    def __init__(self, model):
        self.model = model
        self.count = 0

    def _on_load(self, *a, **k):
        self.count += 1

    def __enter__(self):
        event.listen(self.model, "load", self._on_load)
        return self

    def __exit__(self, *a):
        event.remove(self.model, "load", self._on_load)


class _QueryCounter:
    def __init__(self, db):
        self.engine = db.get_bind()
        self.count = 0

    def _on_exec(self, *a, **k):
        self.count += 1

    def __enter__(self):
        event.listen(self.engine, "after_cursor_execute", self._on_exec)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "after_cursor_execute", self._on_exec)


def _old_algorithm(db, user, *, cadence_state, limit, offset, now):
    """The pre-PERF-10 algorithm: full fetch, classify in Python, filter, slice.

    Reproduced verbatim so the optimized code can be asserted byte-for-byte identical.
    """
    base = customer_contacts_query(db, user)
    rows = base.all()
    for c in rows:
        c.cadence_state = cadence_state_of(c, now)
    rows = [c for c in rows if c.cadence_state == cadence_state]
    total = len(rows)
    return rows[offset : offset + limit], total


# ─────────────────────────────────────────────────────────────────────────────
# 1. Performance guard — cadence filter pages in SQL, cost independent of N
# ─────────────────────────────────────────────────────────────────────────────


def test_cadence_filter_pages_in_sql_cost_independent_of_n(db_session: Session, admin_user: User):
    """Only the LIMIT-sized page is fetched; doubling the matching set does not change
    it."""
    admin_id = admin_user.id
    _, site = _company_site(db_session)
    site_id = site.id
    now = datetime.now(timezone.utc)

    for i in range(25):
        _contact(
            db_session,
            site_id,
            name=f"Overdue {i}",
            last_outbound=now - timedelta(days=60),  # unambiguously overdue
            last_activity=now - timedelta(days=i),  # distinct → stable order
        )
    db_session.commit()
    db_session.expunge_all()  # force real DB loads so the load event fires per fetched row

    admin = db_session.get(User, admin_id)
    with _QueryCounter(db_session) as qc, _LoadCounter(SiteContact) as lc:
        ctx = customer_contacts_list_ctx(db_session, admin, cadence_state="overdue", limit=5, offset=0)

    assert ctx["total"] == 25, "count must reflect the full filtered set"
    assert len(ctx["contacts"]) == 5, "page must honor the limit"
    # PERF-10 core: only the 5-row page materializes, NOT all 25 filtered rows.
    assert lc.count == 5, f"expected 5 SiteContact rows loaded (page size), got {lc.count}"
    queries_at_25 = qc.count

    # Double the matching population; page cost (queries + rows loaded) must stay flat.
    for i in range(25, 50):
        _contact(
            db_session,
            site_id,
            name=f"Overdue {i}",
            last_outbound=now - timedelta(days=60),
            last_activity=now - timedelta(days=i),
        )
    db_session.commit()
    db_session.expunge_all()

    admin = db_session.get(User, admin_id)
    with _QueryCounter(db_session) as qc2, _LoadCounter(SiteContact) as lc2:
        ctx2 = customer_contacts_list_ctx(db_session, admin, cadence_state="overdue", limit=5, offset=0)

    assert ctx2["total"] == 50
    assert lc2.count == 5, f"page cost scaled with N: {lc2.count} rows loaded for 50 matches"
    assert qc2.count == queries_at_25, "query count must not scale with the filtered-set size"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Exact equivalence — SQL predicate == Python classifier at a fixed `now`
# ─────────────────────────────────────────────────────────────────────────────


def test_predicate_matches_python_classifier_across_edges(db_session: Session, admin_user: User):
    """contact_cadence_predicate partitions rows exactly like cadence_state_of,
    including NULL, future-dated, and every day-boundary around the 30/31-day
    threshold."""
    _, site = _company_site(db_session)
    site_id = site.id
    now = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)

    edges = {
        "null": None,
        "future_2d": now + timedelta(days=2),
        "d0": now,
        "d5": now - timedelta(days=5),
        "d29": now - timedelta(days=29),
        "d30": now - timedelta(days=30),
        "d30_12h": now - timedelta(days=30, hours=12),
        "d31": now - timedelta(days=31),
        "d31_12h": now - timedelta(days=31, hours=12),
        "d60": now - timedelta(days=60),
    }
    for label, ts in edges.items():
        _contact(db_session, site_id, name=label, last_outbound=ts)
    db_session.commit()

    base = customer_contacts_query(db_session, admin_user)
    all_contacts = base.all()

    seen: set[int] = set()
    for state in CONTACT_CADENCE_DOTS:
        sql_ids = {c.id for c in base.filter(contact_cadence_predicate(state, now)).all()}
        py_ids = {c.id for c in all_contacts if cadence_state_of(c, now) == state}
        assert sql_ids == py_ids, f"state={state}: SQL {sql_ids} != Python {py_ids}"
        assert not (seen & sql_ids), f"state={state} overlaps a prior band"
        seen |= sql_ids

    # Every contact is classified into exactly one band (total partition).
    assert seen == {c.id for c in all_contacts}


def test_due_band_is_empty_for_contacts(db_session: Session, admin_user: User):
    """With standard target == the red ceiling, the 'due' band is unreachable for
    contacts (tier=None) — the SQL predicate must agree by returning nothing."""
    assert TIER_TARGET_DAYS.get("standard") == CADENCE_RED_DAYS  # invariant the empty band relies on
    _, site = _company_site(db_session)
    site_id = site.id
    now = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    for d in (0, 15, 30, 31, 45, 90):
        _contact(db_session, site_id, name=f"d{d}", last_outbound=now - timedelta(days=d))
    db_session.commit()

    base = customer_contacts_query(db_session, admin_user)
    assert base.filter(contact_cadence_predicate("due", now)).all() == []
    assert not [c for c in base.all() if cadence_state_of(c, now) == "due"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Paged-ctx equivalence — same ids / order / total as the pre-fix algorithm
# ─────────────────────────────────────────────────────────────────────────────


def test_ctx_matches_old_algorithm_including_duplicates_and_nulls(db_session: Session, admin_user: User):
    """The optimized ctx returns identical ids, order and totals to the full-fetch-then-
    slice algorithm across pages, duplicate sort keys, and the empty 'due' band."""
    _, site = _company_site(db_session)
    site_id = site.id
    now = datetime.now(timezone.utc)
    tie = now - timedelta(days=100)  # identical last_activity_at → id.desc() tie-break

    # 6 overdue: 3 with distinct activity times, 3 sharing `tie` (duplicate sort keys).
    for i in range(3):
        _contact(
            db_session,
            site_id,
            name=f"OD-distinct-{i}",
            last_outbound=now - timedelta(days=60),
            last_activity=now - timedelta(days=10 + i),
        )
    for i in range(3):
        _contact(db_session, site_id, name=f"OD-tie-{i}", last_outbound=now - timedelta(days=60), last_activity=tie)
    # 3 on_target, 2 new (NULL outbound).
    for i in range(3):
        _contact(
            db_session,
            site_id,
            name=f"OT-{i}",
            last_outbound=now - timedelta(days=5),
            last_activity=now - timedelta(days=i),
        )
    for i in range(2):
        _contact(db_session, site_id, name=f"NEW-{i}", last_outbound=None, last_activity=now - timedelta(days=i))
    db_session.commit()

    cases = [
        ("overdue", 2, 0),
        ("overdue", 2, 2),
        ("overdue", 2, 4),
        ("overdue", 50, 0),
        ("on_target", 2, 0),
        ("on_target", 2, 2),
        ("new", 50, 0),
        ("due", 50, 0),  # empty band
    ]
    for state, limit, offset in cases:
        old_page, old_total = _old_algorithm(
            db_session, admin_user, cadence_state=state, limit=limit, offset=offset, now=now
        )
        ctx = customer_contacts_list_ctx(db_session, admin_user, cadence_state=state, limit=limit, offset=offset)
        assert ctx["total"] == old_total, f"{state} l={limit} o={offset}: total mismatch"
        assert [c.id for c in ctx["contacts"]] == [c.id for c in old_page], (
            f"{state} l={limit} o={offset}: page ids/order mismatch"
        )
        for c in ctx["contacts"]:
            assert c.cadence_state == state, "returned rows must carry the requested cadence_state"


# ─────────────────────────────────────────────────────────────────────────────
# 4. CSV export call-site — same filtered rows, now streamed (no full materialize)
# ─────────────────────────────────────────────────────────────────────────────


def _export_names(db, user, *, cadence_state) -> set[str]:
    import csv as _csv
    import io as _io

    from app.routers.crm.export import _contacts_generator

    text = "".join(_contacts_generator(db, user, search="", company_id=0, contact_role="", cadence_state=cadence_state))
    reader = _csv.DictReader(_io.StringIO(text))
    return {row["full_name"] for row in reader}


def test_contacts_csv_export_cadence_filter_matches_python(db_session: Session, admin_user: User):
    """The export generator's cadence branch yields exactly the rows the old Python
    filter did."""
    _, site = _company_site(db_session)
    site_id = site.id
    now = datetime.now(timezone.utc)
    for i in range(4):
        _contact(db_session, site_id, name=f"OD-{i}", last_outbound=now - timedelta(days=60))
    for i in range(3):
        _contact(db_session, site_id, name=f"OT-{i}", last_outbound=now - timedelta(days=5))
    for i in range(2):
        _contact(db_session, site_id, name=f"NEW-{i}", last_outbound=None)
    db_session.commit()

    base = customer_contacts_query(db_session, admin_user)
    all_contacts = base.all()
    for state in ("overdue", "on_target", "new"):
        expected = {c.full_name for c in all_contacts if cadence_state_of(c, now) == state}
        assert _export_names(db_session, admin_user, cadence_state=state) == expected, f"export {state} mismatch"

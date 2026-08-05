"""Tests for load test performance fixes and correct offer model.

Offer model: "active" (default) or "sold" (manually marked).
is_stale is display-only metadata — never hides offers.
"Leave no stone unturned."

Called by: pytest tests/test_load_test_fixes.py
Depends on: app/routers/crm/offers.py,
            app/routers/crm/companies.py, app/scheduler.py,
            app/routers/dashboard.py
"""

import os
from datetime import UTC, datetime, timedelta

from app.models import Offer, Requisition, User

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db, name="Test User"):
    u = User(name=name, email=f"{name.lower().replace(' ', '.')}@test.com")
    db.add(u)
    db.flush()
    return u


def _make_req(db, user, name="REQ-1", status="open"):
    r = Requisition(
        name=name,
        status=status,
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(r)
    db.flush()
    return r


def _make_offer(db, req, user, status="active", mpn="LM317T", days_ago=0):
    o = Offer(
        requisition_id=req.id,
        vendor_name="Arrow",
        mpn=mpn,
        qty_available=100,
        unit_price=1.50,
        entered_by_id=user.id,
        status=status,
        created_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    db.add(o)
    db.flush()
    return o


# ── Fix 1: Migration file exists ────────────────────────────────────────


def _load_migration_015():
    import importlib.util

    path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "alembic",
        "versions",
        "015_performance_indexes.py",
    )
    assert os.path.exists(path)
    spec = importlib.util.spec_from_file_location("migration_015", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestPerformanceIndexes:
    def test_migration_file_exists(self):
        """Migration 015 should exist with is_stale column."""
        mod = _load_migration_015()
        assert mod.revision == "015_performance_indexes"
        assert mod.down_revision == "014_multiplier_score_snapshot"

    def test_migration_has_upgrade_and_downgrade(self):
        mod = _load_migration_015()
        assert callable(mod.upgrade)
        assert callable(mod.downgrade)


# NOTE: the "Fix 2: Companies typeahead caching" class formerly here tested
# GET /api/companies/typeahead, which was removed as dead code (its only caller,
# unified_modal.html's customerPicker(), moved to the server-rendered
# GET /v2/partials/requisitions/customer-typeahead in P5.2 — see
# TestCustomerTypeaheadDropdown-style coverage in tests/test_unified_req_form.py and
# tests/test_htmx_views.py for the equivalent active/site-filtering assertions on the
# live route). /api/autocomplete/names remains live and untouched.


# ── Stale flag ──────────────────────────────────────────────────────────


class TestStaleFlag:
    def test_is_stale_default_false(self, db_session, test_user):
        """New offers should have is_stale=False by default."""
        req = _make_req(db_session, test_user)
        o = _make_offer(db_session, req, test_user)
        db_session.commit()
        db_session.refresh(o)
        assert o.is_stale is False

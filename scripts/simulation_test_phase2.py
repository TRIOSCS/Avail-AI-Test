"""Simulation tests for AVAIL v1.3.0 Phase 2 — Customer Account Ownership.

Tests: ownership sweep logic, day-23 alerts, open pool claim, sales dashboard
       endpoints, auto-claim wiring, manager digest.

Run: python3 scripts/simulation_test_phase2.py
"""
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        msg = f"  ✗ {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


# ═══════════════════════════════════════════════════════════════════════
#  Category 1: Ownership Service — Functions Exist
# ═══════════════════════════════════════════════════════════════════════

def test_ownership_functions():
    print("\n── 1. Ownership Service Functions ──")
    import inspect
    from app.services.ownership_service import (
        run_ownership_sweep,
        check_and_claim_open_account,
        get_accounts_at_risk,
        get_open_pool_accounts,
        get_my_accounts,
        get_manager_digest,
        send_manager_digest_email,
    )

    check("run_ownership_sweep is async", inspect.iscoroutinefunction(run_ownership_sweep))
    check("check_and_claim_open_account is sync", not inspect.iscoroutinefunction(check_and_claim_open_account))
    check("get_accounts_at_risk is sync", not inspect.iscoroutinefunction(get_accounts_at_risk))
    check("get_open_pool_accounts is sync", not inspect.iscoroutinefunction(get_open_pool_accounts))
    check("get_my_accounts is sync", not inspect.iscoroutinefunction(get_my_accounts))
    check("get_manager_digest is sync", not inspect.iscoroutinefunction(get_manager_digest))
    check("send_manager_digest_email is async", inspect.iscoroutinefunction(send_manager_digest_email))


# ═══════════════════════════════════════════════════════════════════════
#  Category 2: Days Since Activity Calculation
# ═══════════════════════════════════════════════════════════════════════

def test_days_since_activity():
    print("\n── 2. Days Since Activity ──")
    from app.services.ownership_service import _days_since_activity

    now = datetime.now(timezone.utc)

    # No activity → None
    company = MagicMock()
    company.last_activity_at = None
    check("No activity returns None", _days_since_activity(company, now) is None)

    # Activity 5 days ago → 5
    company.last_activity_at = now - timedelta(days=5)
    result = _days_since_activity(company, now)
    check("5 days ago returns 5", result == 5, f"got {result}")

    # Activity today → 0
    company.last_activity_at = now - timedelta(hours=2)
    result = _days_since_activity(company, now)
    check("2 hours ago returns 0", result == 0, f"got {result}")

    # Activity 25 days ago → 25
    company.last_activity_at = now - timedelta(days=25)
    result = _days_since_activity(company, now)
    check("25 days ago returns 25", result == 25, f"got {result}")

    # Naive datetime handling
    company.last_activity_at = (now - timedelta(days=10)).replace(tzinfo=None)
    result = _days_since_activity(company, now)
    check("Naive datetime handled correctly", result == 10, f"got {result}")


# ═══════════════════════════════════════════════════════════════════════
#  Category 3: Clear Ownership
# ═══════════════════════════════════════════════════════════════════════

def test_clear_ownership():
    print("\n── 3. Clear Ownership ──")
    from app.services.ownership_service import _clear_ownership

    company = MagicMock()
    company.account_owner_id = 42
    db = MagicMock()

    _clear_ownership(company, db)

    check("account_owner_id cleared to None", company.account_owner_id is None)
    check("ownership_cleared_at set", company.ownership_cleared_at is not None)
    check("db.flush called", db.flush.called)


# ═══════════════════════════════════════════════════════════════════════
#  Category 4: Open Pool Claim
# ═══════════════════════════════════════════════════════════════════════

def test_claim_open_account():
    print("\n── 4. Open Pool Claim ──")
    from app.services.ownership_service import check_and_claim_open_account

    # Company not found → False
    db = MagicMock()
    db.query.return_value.get.return_value = None
    check("Missing company returns False", check_and_claim_open_account(999, 1, db) is False)

    # Company already owned → False
    company = MagicMock()
    company.account_owner_id = 42
    db = MagicMock()
    db.query.return_value.get.return_value = company
    check("Owned company returns False", check_and_claim_open_account(1, 1, db) is False)

    # Unowned company, non-sales user → False
    company = MagicMock()
    company.account_owner_id = None
    user = MagicMock()
    user.role = "buyer"
    db = MagicMock()
    db.query.return_value.get.side_effect = [company, user]
    check("Buyer cannot claim", check_and_claim_open_account(1, 1, db) is False)

    # Unowned company, sales user → True + ownership assigned
    company = MagicMock()
    company.account_owner_id = None
    user = MagicMock()
    user.role = "sales"
    user.name = "Test Sales"
    db = MagicMock()
    db.query.return_value.get.side_effect = [company, user]
    result = check_and_claim_open_account(1, 5, db)
    check("Sales user claims successfully", result is True)
    check("Owner ID set to user", company.account_owner_id == 5)
    check("ownership_cleared_at nulled", company.ownership_cleared_at is None)


# ═══════════════════════════════════════════════════════════════════════
#  Category 5: Was Warned Today Check
# ═══════════════════════════════════════════════════════════════════════

def test_warned_today():
    print("\n── 5. Warned Today Check ──")
    from app.services.ownership_service import _was_warned_today

    # No warning today → False
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    check("No warning returns False", _was_warned_today(1, 1, db) is False)

    # Warning exists → True
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock()
    check("Existing warning returns True", _was_warned_today(1, 1, db) is True)


# ═══════════════════════════════════════════════════════════════════════
#  Category 6: Activity Service Auto-Claim Wiring
# ═══════════════════════════════════════════════════════════════════════

def test_autoclaim_wiring():
    print("\n── 6. Auto-Claim Wiring ──")
    import inspect
    from app.services.activity_service import _update_last_activity

    sig = inspect.signature(_update_last_activity)
    params = list(sig.parameters.keys())

    check("_update_last_activity has user_id param", "user_id" in params)
    check("user_id defaults to None", sig.parameters["user_id"].default is None)

    # Check that log_email_activity passes user_id to _update_last_activity
    source = Path("app/services/activity_service.py").read_text()
    check("Email logging passes user_id", "_update_last_activity(match, db, user_id)" in source)

    # Verify the import of ownership_service is in _update_last_activity
    check("Imports check_and_claim_open_account", "check_and_claim_open_account" in source)


# ═══════════════════════════════════════════════════════════════════════
#  Category 7: Config Window Values
# ═══════════════════════════════════════════════════════════════════════

def test_config_windows():
    print("\n── 7. Config Windows ──")
    from app.config import settings

    # Standard: 30 days, warning at 23
    check("Standard window: 30 days", settings.customer_inactivity_days == 30)
    check("Warning at day 23", settings.customer_warning_days == 23)
    check("7-day gap: 30 - 23 = 7", settings.customer_inactivity_days - settings.customer_warning_days == 7)

    # Strategic: 90 days
    check("Strategic window: 90 days", settings.strategic_inactivity_days == 90)
    check("Strategic warning at day 83", settings.strategic_inactivity_days - 7 == 83)


# ═══════════════════════════════════════════════════════════════════════
#  Category 8: Sales Dashboard Endpoints
# ═══════════════════════════════════════════════════════════════════════

def test_sales_endpoints():
    print("\n── 8. Sales Dashboard Endpoints ──")
    source = Path("app/main.py").read_text()

    check("GET /api/sales/my-accounts", '"/api/sales/my-accounts"' in source)
    check("GET /api/sales/at-risk", '"/api/sales/at-risk"' in source)
    check("GET /api/sales/open-pool", '"/api/sales/open-pool"' in source)
    check("POST /api/sales/claim/{company_id}", '"/api/sales/claim/{company_id}"' in source)
    check("PUT /api/companies/{company_id}/strategic", '"/api/companies/{company_id}/strategic"' in source)
    check("GET /api/sales/manager-digest", '"/api/sales/manager-digest"' in source)
    check("GET /api/sales/notifications", '"/api/sales/notifications"' in source)


# ═══════════════════════════════════════════════════════════════════════
#  Category 9: Scheduler Integration
# ═══════════════════════════════════════════════════════════════════════

def test_scheduler_integration():
    print("\n── 9. Scheduler Integration ──")
    source = Path("app/scheduler.py").read_text()

    check("Imports run_ownership_sweep", "run_ownership_sweep" in source)
    check("Gated by activity_tracking_enabled", source.count("activity_tracking_enabled") >= 2)  # webhooks + sweep
    check("Has _last_ownership_sweep tracker", "_last_ownership_sweep" in source)
    check("12-hour sweep interval", "hours=12" in source)


# ═══════════════════════════════════════════════════════════════════════
#  Category 10: Warning Alert Content
# ═══════════════════════════════════════════════════════════════════════

def test_warning_alert():
    print("\n── 10. Warning Alert Content ──")
    import inspect
    from app.services.ownership_service import _send_warning_alert

    check("_send_warning_alert is async", inspect.iscoroutinefunction(_send_warning_alert))

    # Check that the alert function creates an activity_log record
    source = Path("app/services/ownership_service.py").read_text()
    check("Warning logged as activity_type='ownership_warning'", '"ownership_warning"' in source)
    check("Alert email uses Graph API sendMail", "sendMail" in source)
    check("Email includes company link", "/companies/" in source)
    check("Email includes days remaining", "days_remaining" in source)
    check("saveToSentItems=false for system alerts", '"saveToSentItems": "false"' in source)


# ═══════════════════════════════════════════════════════════════════════
#  Category 11: Manager Digest
# ═══════════════════════════════════════════════════════════════════════

def test_manager_digest():
    print("\n── 11. Manager Digest ──")
    source = Path("app/services/ownership_service.py").read_text()

    check("Digest includes at_risk_count", "at_risk_count" in source)
    check("Digest includes recently_cleared", "recently_cleared" in source)
    check("Digest includes team_activity", "team_activity" in source)
    check("Digest email has account table", "<table" in source)
    check("Digest sends to admin_emails", "admin_emails" in source)


# ═══════════════════════════════════════════════════════════════════════
#  Category 12: Open Pool Query
# ═══════════════════════════════════════════════════════════════════════

def test_open_pool_query():
    print("\n── 12. Open Pool Query ──")
    from app.services.ownership_service import get_open_pool_accounts

    # Mock: returns companies with no owner
    db = MagicMock()
    mock_company = MagicMock()
    mock_company.id = 1
    mock_company.name = "Test Corp"
    mock_company.ownership_cleared_at = None
    mock_company.last_activity_at = None
    mock_company.is_strategic = False
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [mock_company]

    result = get_open_pool_accounts(db)
    check("Returns list", isinstance(result, list))
    check("First result has company_id", result[0]["company_id"] == 1)
    check("First result has company_name", result[0]["company_name"] == "Test Corp")
    check("First result has is_strategic", "is_strategic" in result[0])


# ═══════════════════════════════════════════════════════════════════════
#  Category 13: My Accounts Query
# ═══════════════════════════════════════════════════════════════════════

def test_my_accounts_query():
    print("\n── 13. My Accounts Query ──")
    from app.services.ownership_service import get_my_accounts

    now = datetime.now(timezone.utc)

    # Green status: active within window
    db = MagicMock()
    company = MagicMock()
    company.id = 1
    company.name = "Healthy Corp"
    company.is_strategic = False
    company.last_activity_at = now - timedelta(days=5)
    company.created_at = now - timedelta(days=60)
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [company]

    result = get_my_accounts(1, db)
    check("Returns list", isinstance(result, list))
    check("Green status for recent activity", result[0]["status"] == "green", f"got {result[0]['status']}")

    # Yellow status: in warning zone
    company.last_activity_at = now - timedelta(days=25)
    result = get_my_accounts(1, db)
    check("Yellow status for warning zone", result[0]["status"] == "yellow", f"got {result[0]['status']}")

    # No activity status
    company.last_activity_at = None
    result = get_my_accounts(1, db)
    check("no_activity status when none", result[0]["status"] == "no_activity", f"got {result[0]['status']}")


# ═══════════════════════════════════════════════════════════════════════
#  Category 14: Strategic Account Extended Window
# ═══════════════════════════════════════════════════════════════════════

def test_strategic_window():
    print("\n── 14. Strategic Account Window ──")
    from app.services.ownership_service import get_my_accounts

    now = datetime.now(timezone.utc)
    db = MagicMock()

    # 35 days inactive + strategic → should be green (90-day window, warning at 83)
    company = MagicMock()
    company.id = 1
    company.name = "Strategic Corp"
    company.is_strategic = True
    company.last_activity_at = now - timedelta(days=35)
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [company]

    result = get_my_accounts(1, db)
    check("Strategic 35 days = green", result[0]["status"] == "green", f"got {result[0]['status']}")
    check("Inactivity limit = 90", result[0]["inactivity_limit"] == 90, f"got {result[0]['inactivity_limit']}")

    # 85 days inactive + strategic → yellow (warning zone: 83-90)
    company.last_activity_at = now - timedelta(days=85)
    result = get_my_accounts(1, db)
    check("Strategic 85 days = yellow", result[0]["status"] == "yellow", f"got {result[0]['status']}")

    # 35 days inactive + non-strategic → red (past 30-day limit)
    company.is_strategic = False
    company.last_activity_at = now - timedelta(days=35)
    result = get_my_accounts(1, db)
    check("Non-strategic 35 days = red", result[0]["status"] == "red", f"got {result[0]['status']}")


# ═══════════════════════════════════════════════════════════════════════
#  Category 15: Claim Endpoint Access Control
# ═══════════════════════════════════════════════════════════════════════

def test_claim_access_control():
    print("\n── 15. Claim Access Control ──")
    source = Path("app/main.py").read_text()

    # Verify role check exists in the claim endpoint
    check("Claim checks role == sales", '"Only sales users can claim accounts"' in source)
    check("Claim checks if already owned", "already owned" in source.lower())
    check("Strategic toggle checks admin", "Admin only" in source)
    check("Manager digest checks admin", source.count("Admin only") >= 2)


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        test_ownership_functions,
        test_days_since_activity,
        test_clear_ownership,
        test_claim_open_account,
        test_warned_today,
        test_autoclaim_wiring,
        test_config_windows,
        test_sales_endpoints,
        test_scheduler_integration,
        test_warning_alert,
        test_manager_digest,
        test_open_pool_query,
        test_my_accounts_query,
        test_strategic_window,
        test_claim_access_control,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except Exception as e:
            FAIL += 1
            msg = f"  ✗ {test_fn.__name__} CRASHED: {e}"
            print(msg)
            ERRORS.append(msg)
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"Phase 2 Results: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    print(f"{'='*60}")

    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
        sys.exit(1)
    else:
        print("\n✅ All Phase 2 tests passed!")
        sys.exit(0)

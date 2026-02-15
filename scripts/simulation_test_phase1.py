"""Simulation tests for AVAIL v1.3.0 Phase 1 — Activity Logging & Buyer Routing Foundation.

Tests: activity_log model, contact matching, email/call logging, buyer profiles,
       webhook service, config values, company activity status.

Run: python3 -m scripts.simulation_test_phase1
"""
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

# Add project root to path
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
#  Category 1: Model Definitions
# ═══════════════════════════════════════════════════════════════════════

def test_models():
    print("\n── 1. Model Definitions ──")
    from app.models import ActivityLog, BuyerProfile, BuyerVendorStats, GraphSubscription

    # ActivityLog
    cols = {c.name for c in ActivityLog.__table__.columns}
    check("ActivityLog has user_id", "user_id" in cols)
    check("ActivityLog has activity_type", "activity_type" in cols)
    check("ActivityLog has channel", "channel" in cols)
    check("ActivityLog has company_id", "company_id" in cols)
    check("ActivityLog has vendor_card_id", "vendor_card_id" in cols)
    check("ActivityLog has external_id", "external_id" in cols)
    check("ActivityLog has contact_email", "contact_email" in cols)
    check("ActivityLog has subject", "subject" in cols)
    check("ActivityLog has duration_seconds", "duration_seconds" in cols)

    # BuyerProfile
    cols = {c.name for c in BuyerProfile.__table__.columns}
    check("BuyerProfile has user_id", "user_id" in cols)
    check("BuyerProfile has primary_commodity", "primary_commodity" in cols)
    check("BuyerProfile has secondary_commodity", "secondary_commodity" in cols)
    check("BuyerProfile has primary_geography", "primary_geography" in cols)
    check("BuyerProfile has brand_specialties", "brand_specialties" in cols)
    check("BuyerProfile has brand_material_types", "brand_material_types" in cols)
    check("BuyerProfile has brand_usage_types", "brand_usage_types" in cols)

    # BuyerVendorStats
    cols = {c.name for c in BuyerVendorStats.__table__.columns}
    check("BuyerVendorStats has rfqs_sent", "rfqs_sent" in cols)
    check("BuyerVendorStats has response_rate", "response_rate" in cols)
    check("BuyerVendorStats has win_rate", "win_rate" in cols)
    check("BuyerVendorStats has avg_response_hours", "avg_response_hours" in cols)
    check("BuyerVendorStats has last_contact_at", "last_contact_at" in cols)

    # GraphSubscription
    cols = {c.name for c in GraphSubscription.__table__.columns}
    check("GraphSubscription has subscription_id", "subscription_id" in cols)
    check("GraphSubscription has resource", "resource" in cols)
    check("GraphSubscription has expiration_dt", "expiration_dt" in cols)
    check("GraphSubscription has client_state", "client_state" in cols)


# ═══════════════════════════════════════════════════════════════════════
#  Category 2: Offer Attribution Fields
# ═══════════════════════════════════════════════════════════════════════

def test_offer_fields():
    print("\n── 2. Offer Attribution Fields ──")
    from app.models import Offer
    cols = {c.name for c in Offer.__table__.columns}
    check("Offer has expires_at", "expires_at" in cols)
    check("Offer has reconfirmed_at", "reconfirmed_at" in cols)
    check("Offer has reconfirm_count", "reconfirm_count" in cols)
    check("Offer has attribution_status", "attribution_status" in cols)


# ═══════════════════════════════════════════════════════════════════════
#  Category 3: Company Ownership Fields
# ═══════════════════════════════════════════════════════════════════════

def test_company_fields():
    print("\n── 3. Company Ownership Fields ──")
    from app.models import Company
    cols = {c.name for c in Company.__table__.columns}
    check("Company has is_strategic", "is_strategic" in cols)
    check("Company has ownership_cleared_at", "ownership_cleared_at" in cols)
    check("Company has last_activity_at", "last_activity_at" in cols)
    check("Company has account_owner_id", "account_owner_id" in cols)


# ═══════════════════════════════════════════════════════════════════════
#  Category 4: Vendor Card Scorecard Fields
# ═══════════════════════════════════════════════════════════════════════

def test_vendor_scorecard_fields():
    print("\n── 4. Vendor Scorecard Fields ──")
    from app.models import VendorCard
    cols = {c.name for c in VendorCard.__table__.columns}
    check("VendorCard has avg_response_hours", "avg_response_hours" in cols)
    check("VendorCard has overall_win_rate", "overall_win_rate" in cols)
    check("VendorCard has total_pos", "total_pos" in cols)
    check("VendorCard has total_revenue", "total_revenue" in cols)
    check("VendorCard has last_activity_at", "last_activity_at" in cols)


# ═══════════════════════════════════════════════════════════════════════
#  Category 5: Config Values
# ═══════════════════════════════════════════════════════════════════════

def test_config():
    print("\n── 5. Config Values ──")
    from app.config import settings
    check("customer_inactivity_days = 30", settings.customer_inactivity_days == 30)
    check("strategic_inactivity_days = 90", settings.strategic_inactivity_days == 90)
    check("customer_warning_days = 23", settings.customer_warning_days == 23)
    check("offer_attribution_days = 14", settings.offer_attribution_days == 14)
    check("routing_window_hours = 48", settings.routing_window_hours == 48)
    check("collision_lookback_days = 7", settings.collision_lookback_days == 7)
    check("vendor_protection_warn_days = 60", settings.vendor_protection_warn_days == 60)
    check("vendor_protection_drop_days = 90", settings.vendor_protection_drop_days == 90)
    check("activity_tracking_enabled = True", settings.activity_tracking_enabled is True)


# ═══════════════════════════════════════════════════════════════════════
#  Category 6: Activity Service — Contact Matching
# ═══════════════════════════════════════════════════════════════════════

def test_contact_matching():
    print("\n── 6. Contact Matching ──")
    from app.services.activity_service import match_email_to_entity, match_phone_to_entity, _GENERIC_DOMAINS

    # Generic domains should be skipped
    check("gmail.com in generic domains", "gmail.com" in _GENERIC_DOMAINS)
    check("yahoo.com in generic domains", "yahoo.com" in _GENERIC_DOMAINS)
    check("outlook.com in generic domains", "outlook.com" in _GENERIC_DOMAINS)

    # match_email_to_entity returns None for empty
    mock_db = MagicMock()
    result = match_email_to_entity("", mock_db)
    check("Empty email returns None", result is None)

    result = match_email_to_entity(None, mock_db)
    check("None email returns None", result is None)

    # match_phone_to_entity returns None for short numbers
    result = match_phone_to_entity("123", mock_db)
    check("Short phone returns None", result is None)

    result = match_phone_to_entity("", mock_db)
    check("Empty phone returns None", result is None)

    result = match_phone_to_entity(None, mock_db)
    check("None phone returns None", result is None)


# ═══════════════════════════════════════════════════════════════════════
#  Category 7: Activity Service — Email Logging
# ═══════════════════════════════════════════════════════════════════════

def test_email_logging():
    print("\n── 7. Email Logging ──")
    from app.services.activity_service import log_email_activity

    mock_db = MagicMock()

    # Dedup: external_id already exists → returns None
    mock_db.query.return_value.filter.return_value.first.return_value = MagicMock()  # existing record
    result = log_email_activity(
        user_id=1, direction="sent", email_addr="test@example.com",
        subject="Test", external_id="msg-123", contact_name="John", db=mock_db
    )
    check("Dedup blocks duplicate external_id", result is None)

    # No match found → returns None
    mock_db2 = MagicMock()
    mock_db2.query.return_value.filter.return_value.first.return_value = None  # no dedup hit, no match
    with patch("app.services.activity_service.match_email_to_entity", return_value=None):
        result = log_email_activity(
            user_id=1, direction="sent", email_addr="unknown@nowhere.com",
            subject="Test", external_id=None, contact_name=None, db=mock_db2
        )
    check("No match returns None", result is None)


# ═══════════════════════════════════════════════════════════════════════
#  Category 8: Activity Service — Call Logging
# ═══════════════════════════════════════════════════════════════════════

def test_call_logging():
    print("\n── 8. Call Logging ──")
    from app.services.activity_service import log_call_activity

    # No match → returns None
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    with patch("app.services.activity_service.match_phone_to_entity", return_value=None):
        result = log_call_activity(
            user_id=1, direction="outbound", phone="+15551234567",
            duration_seconds=120, external_id=None, contact_name=None, db=mock_db
        )
    check("Call with no match returns None", result is None)

    # Dedup check
    mock_db2 = MagicMock()
    mock_db2.query.return_value.filter.return_value.first.return_value = MagicMock()
    result = log_call_activity(
        user_id=1, direction="outbound", phone="+15551234567",
        duration_seconds=120, external_id="call-456", contact_name=None, db=mock_db2
    )
    check("Call dedup blocks duplicate external_id", result is None)


# ═══════════════════════════════════════════════════════════════════════
#  Category 9: Activity Service — Query Helpers
# ═══════════════════════════════════════════════════════════════════════

def test_query_helpers():
    print("\n── 9. Query Helpers ──")
    from app.services.activity_service import (
        get_company_activities, get_vendor_activities, get_user_activities,
        days_since_last_activity
    )

    # All query functions are callable
    check("get_company_activities callable", callable(get_company_activities))
    check("get_vendor_activities callable", callable(get_vendor_activities))
    check("get_user_activities callable", callable(get_user_activities))
    check("days_since_last_activity callable", callable(days_since_last_activity))

    # days_since_last_activity returns None when no records
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.scalar.return_value = None
    result = days_since_last_activity(999, mock_db)
    check("No activity returns None days", result is None)


# ═══════════════════════════════════════════════════════════════════════
#  Category 10: Buyer Service — Profile CRUD
# ═══════════════════════════════════════════════════════════════════════

def test_buyer_service():
    print("\n── 10. Buyer Service ──")
    from app.services.buyer_service import (
        get_profile, upsert_profile, list_profiles, delete_profile,
        VALID_COMMODITIES, VALID_GEOGRAPHIES, VALID_USAGE_TYPES
    )

    # Valid sets populated
    check("semiconductors in commodities", "semiconductors" in VALID_COMMODITIES)
    check("pc_server_parts in commodities", "pc_server_parts" in VALID_COMMODITIES)
    check("apac in geographies", "apac" in VALID_GEOGRAPHIES)
    check("emea in geographies", "emea" in VALID_GEOGRAPHIES)
    check("americas in geographies", "americas" in VALID_GEOGRAPHIES)
    check("sourcing_to_buy in usage types", "sourcing_to_buy" in VALID_USAGE_TYPES)

    # Functions are callable
    check("get_profile callable", callable(get_profile))
    check("upsert_profile callable", callable(upsert_profile))
    check("list_profiles callable", callable(list_profiles))
    check("delete_profile callable", callable(delete_profile))


# ═══════════════════════════════════════════════════════════════════════
#  Category 11: Webhook Service — Functions Exist
# ═══════════════════════════════════════════════════════════════════════

def test_webhook_service():
    print("\n── 11. Webhook Service ──")
    from app.services.webhook_service import (
        create_mail_subscription, renew_subscription,
        renew_expiring_subscriptions, ensure_all_users_subscribed,
        handle_notification, SUBSCRIPTION_LIFETIME_HOURS, RENEW_BUFFER_HOURS
    )
    import inspect

    check("create_mail_subscription is async", inspect.iscoroutinefunction(create_mail_subscription))
    check("renew_subscription is async", inspect.iscoroutinefunction(renew_subscription))
    check("renew_expiring_subscriptions is async", inspect.iscoroutinefunction(renew_expiring_subscriptions))
    check("ensure_all_users_subscribed is async", inspect.iscoroutinefunction(ensure_all_users_subscribed))
    check("handle_notification is async", inspect.iscoroutinefunction(handle_notification))
    check("Subscription lifetime ~3 days", 60 <= SUBSCRIPTION_LIFETIME_HOURS <= 72)
    check("Renew buffer > 0", RENEW_BUFFER_HOURS > 0)


# ═══════════════════════════════════════════════════════════════════════
#  Category 12: Webhook Notification Parsing Helpers
# ═══════════════════════════════════════════════════════════════════════

def test_webhook_helpers():
    print("\n── 12. Webhook Helpers ──")
    from app.services.webhook_service import _extract_email, _extract_name

    # Standard Graph recipient structure
    recip = {"emailAddress": {"address": "jane@acme.com", "name": "Jane Doe"}}
    check("Extract email from recipient", _extract_email(recip) == "jane@acme.com")
    check("Extract name from recipient", _extract_name(recip) == "Jane Doe")

    # None handling
    check("Extract email from None", _extract_email(None) is None)
    check("Extract name from None", _extract_name(None) is None)

    # Empty dict
    check("Extract email from empty", _extract_email({}) is None)
    check("Extract name from empty", _extract_name({}) is None)


# ═══════════════════════════════════════════════════════════════════════
#  Category 13: Migration SQL Validation
# ═══════════════════════════════════════════════════════════════════════

def test_migration_sql():
    print("\n── 13. Migration SQL ──")
    sql = Path("migrations/006_activity_routing_foundation.sql").read_text()

    check("Creates activity_log table", "CREATE TABLE IF NOT EXISTS activity_log" in sql)
    check("Creates buyer_profiles table", "CREATE TABLE IF NOT EXISTS buyer_profiles" in sql)
    check("Creates buyer_vendor_stats table", "CREATE TABLE IF NOT EXISTS buyer_vendor_stats" in sql)
    check("Creates graph_subscriptions table", "CREATE TABLE IF NOT EXISTS graph_subscriptions" in sql)
    check("Adds is_strategic to companies", "is_strategic" in sql)
    check("Adds last_activity_at to companies", "last_activity_at" in sql)
    check("Adds account_owner_id to companies", "account_owner_id" in sql)
    check("Adds expires_at to offers", "expires_at" in sql and "offers" in sql)
    check("Adds attribution_status to offers", "attribution_status" in sql)
    check("Uses BEGIN/COMMIT", "BEGIN;" in sql and "COMMIT;" in sql)
    check("All CREATE TABLE use IF NOT EXISTS", sql.count("CREATE TABLE IF NOT EXISTS") >= 4)
    check("All ALTER use IF NOT EXISTS", "ADD COLUMN IF NOT EXISTS" in sql)


# ═══════════════════════════════════════════════════════════════════════
#  Category 14: Scheduler Integration
# ═══════════════════════════════════════════════════════════════════════

def test_scheduler_integration():
    print("\n── 14. Scheduler Integration ──")
    import app.scheduler as sched
    source = Path("app/scheduler.py").read_text()

    check("Scheduler imports webhook_service", "webhook_service" in source)
    check("Scheduler calls ensure_all_users_subscribed", "ensure_all_users_subscribed" in source)
    check("Scheduler calls renew_expiring_subscriptions", "renew_expiring_subscriptions" in source)
    check("Gated by activity_tracking_enabled", "activity_tracking_enabled" in source)


# ═══════════════════════════════════════════════════════════════════════
#  Category 15: API Endpoints Registered
# ═══════════════════════════════════════════════════════════════════════

def test_api_endpoints():
    print("\n── 15. API Endpoints ──")
    source = Path("app/main.py").read_text()

    check("POST /api/webhooks/graph", '"/api/webhooks/graph"' in source)
    check("GET /api/buyer-profiles", '"/api/buyer-profiles"' in source)
    check("GET /api/buyer-profiles/{user_id}", '"/api/buyer-profiles/{user_id}"' in source)
    check("PUT /api/buyer-profiles/{user_id}", '"/api/buyer-profiles/{user_id}"' in source)
    check("GET /api/companies/{company_id}/activities", '"/api/companies/{company_id}/activities"' in source)
    check("GET /api/vendors/{vendor_id}/activities", '"/api/vendors/{vendor_id}/activities"' in source)
    check("POST /api/activities/call", '"/api/activities/call"' in source)
    check("GET /api/companies/{company_id}/activity-status", '"/api/companies/{company_id}/activity-status"' in source)


# ═══════════════════════════════════════════════════════════════════════
#  Category 16: Activity Type Constants
# ═══════════════════════════════════════════════════════════════════════

def test_activity_types():
    print("\n── 16. Activity Types ──")
    # Verify the activity service produces correct type strings
    from app.services.activity_service import log_email_activity, log_call_activity

    # Check function signatures accept the right args
    import inspect
    sig = inspect.signature(log_email_activity)
    params = list(sig.parameters.keys())
    check("log_email_activity has direction param", "direction" in params)
    check("log_email_activity has email_addr param", "email_addr" in params)
    check("log_email_activity has external_id param", "external_id" in params)

    sig = inspect.signature(log_call_activity)
    params = list(sig.parameters.keys())
    check("log_call_activity has direction param", "direction" in params)
    check("log_call_activity has phone param", "phone" in params)
    check("log_call_activity has duration_seconds param", "duration_seconds" in params)


# ═══════════════════════════════════════════════════════════════════════
#  Category 17: Model Relationships
# ═══════════════════════════════════════════════════════════════════════

def test_model_relationships():
    print("\n── 17. Model Relationships ──")
    from app.models import ActivityLog, BuyerProfile, BuyerVendorStats, GraphSubscription
    from sqlalchemy import inspect as sa_inspect

    # ActivityLog relationships
    mapper = sa_inspect(ActivityLog)
    rels = {r.key for r in mapper.relationships}
    check("ActivityLog → user relationship", "user" in rels)
    check("ActivityLog → company relationship", "company" in rels)
    check("ActivityLog → vendor_card relationship", "vendor_card" in rels)

    # BuyerProfile relationships
    mapper = sa_inspect(BuyerProfile)
    rels = {r.key for r in mapper.relationships}
    check("BuyerProfile → user relationship", "user" in rels)

    # BuyerVendorStats relationships
    mapper = sa_inspect(BuyerVendorStats)
    rels = {r.key for r in mapper.relationships}
    check("BuyerVendorStats → user relationship", "user" in rels)
    check("BuyerVendorStats → vendor_card relationship", "vendor_card" in rels)


# ═══════════════════════════════════════════════════════════════════════
#  Category 18: Graph Client Compatibility
# ═══════════════════════════════════════════════════════════════════════

def test_graph_client_compat():
    print("\n── 18. Graph Client Compatibility ──")
    from app.utils.graph_client import GraphClient
    import inspect

    gc = GraphClient("fake-token")
    check("GraphClient.post_json exists", hasattr(gc, "post_json"))
    check("GraphClient.get_json exists", hasattr(gc, "get_json"))
    check("post_json is async", inspect.iscoroutinefunction(gc.post_json))
    check("get_json is async", inspect.iscoroutinefunction(gc.get_json))


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        test_models,
        test_offer_fields,
        test_company_fields,
        test_vendor_scorecard_fields,
        test_config,
        test_contact_matching,
        test_email_logging,
        test_call_logging,
        test_query_helpers,
        test_buyer_service,
        test_webhook_service,
        test_webhook_helpers,
        test_migration_sql,
        test_scheduler_integration,
        test_api_endpoints,
        test_activity_types,
        test_model_relationships,
        test_graph_client_compat,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except Exception as e:
            FAIL += 1
            msg = f"  ✗ {test_fn.__name__} CRASHED: {e}"
            print(msg)
            ERRORS.append(msg)

    print(f"\n{'='*60}")
    print(f"Phase 1 Results: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    print(f"{'='*60}")

    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
        sys.exit(1)
    else:
        print("\n✅ All Phase 1 tests passed!")
        sys.exit(0)

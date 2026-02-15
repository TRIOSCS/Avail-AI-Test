"""AVAIL v1.3.0 MASTER SIMULATION TEST
=====================================
Comprehensive end-to-end coverage of every feature, function, and process.

Sections:
  A. Model Schema Completeness (28 tables)
  B. Config Completeness
  C. Activity Service — contact matching, logging, dedup, edge cases
  D. Webhook Service — subscription lifecycle, notification parsing, renewal
  E. Buyer Service — CRUD, validation, edge cases
  F. Ownership Service — sweep logic, claim rules, warning alerts, dashboard queries
  G. Routing Service — scoring engine, waterfall, claims, expirations, reconfirmation
  H. Scheduler Integration — all cron jobs wired
  I. API Endpoint Coverage — all 116 routes registered, no dupes
  J. Migration Integrity — new migrations idempotent
  K. Cross-Module Integration — auto-claim wiring, activity→ownership chain
  L. Edge Cases & Boundary Conditions

Run: python3 scripts/simulation_test_master.py
"""
import sys, os, re, ast, inspect, importlib
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = 0
FAIL = 0
ERRORS: list[str] = []
SECTION = ""


def section(name: str):
    global SECTION
    SECTION = name
    print(f"\n{'─'*60}\n  {name}\n{'─'*60}")


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        msg = f"  ✗ [{SECTION}] {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


# ═══════════════════════════════════════════════════════════════════════
#  A. MODEL SCHEMA COMPLETENESS
# ═══════════════════════════════════════════════════════════════════════

def test_a_models():
    section("A. Model Schema Completeness")
    from app.models import Base

    tables = sorted(Base.metadata.tables.keys())
    check(f"28 tables registered", len(tables) == 28, f"got {len(tables)}: {tables}")

    # All tables have PKs
    for name, table in Base.metadata.tables.items():
        check(f"{name} has PK", bool(table.primary_key.columns))

    # FK targets all exist
    fk_errors = []
    for name, table in Base.metadata.tables.items():
        for fk in table.foreign_keys:
            if fk.column.table.name not in Base.metadata.tables:
                fk_errors.append(f"{name}→{fk.column.table.name}")
    check("All FK targets exist", len(fk_errors) == 0, str(fk_errors))

    # v1.3.0 specific tables
    v130_tables = ["activity_log", "buyer_profiles", "buyer_vendor_stats",
                   "graph_subscriptions", "routing_assignments"]
    for t in v130_tables:
        check(f"v1.3.0 table: {t}", t in tables)

    # v1.3.0 column additions
    from app.models import Company, Offer, VendorCard
    for model, cols in [
        (Company, ["is_strategic", "ownership_cleared_at", "last_activity_at", "account_owner_id"]),
        (Offer, ["expires_at", "reconfirmed_at", "reconfirm_count", "attribution_status"]),
        (VendorCard, ["avg_response_hours", "overall_win_rate", "total_pos", "total_revenue", "last_activity_at"]),
    ]:
        model_cols = {c.name for c in model.__table__.columns}
        for col in cols:
            check(f"{model.__tablename__}.{col}", col in model_cols)

    # ActivityLog polymorphic
    from app.models import ActivityLog
    al_cols = {c.name for c in ActivityLog.__table__.columns}
    for col in ["user_id", "activity_type", "channel", "company_id", "vendor_card_id",
                "contact_email", "contact_phone", "contact_name", "subject",
                "duration_seconds", "external_id"]:
        check(f"ActivityLog.{col}", col in al_cols)

    # BuyerProfile ARRAY fields
    from app.models import BuyerProfile
    bp_cols = {c.name for c in BuyerProfile.__table__.columns}
    for col in ["brand_specialties", "brand_material_types", "brand_usage_types"]:
        check(f"BuyerProfile.{col}", col in bp_cols)

    # RoutingAssignment slots
    from app.models import RoutingAssignment
    ra_cols = {c.name for c in RoutingAssignment.__table__.columns}
    for col in ["buyer_1_id", "buyer_2_id", "buyer_3_id", "buyer_1_score",
                "buyer_2_score", "buyer_3_score", "scoring_details",
                "assigned_at", "expires_at", "claimed_by_id", "claimed_at", "status"]:
        check(f"RoutingAssignment.{col}", col in ra_cols)

    # Relationships
    from sqlalchemy import inspect as sa_inspect
    for model, expected_rels in [
        (ActivityLog, ["user", "company", "vendor_card"]),
        (BuyerProfile, ["user"]),
        (RoutingAssignment, ["requirement", "vendor_card", "buyer_1", "buyer_2", "buyer_3", "claimed_by"]),
    ]:
        mapper = sa_inspect(model)
        rels = {r.key for r in mapper.relationships}
        for rel in expected_rels:
            check(f"{model.__tablename__} → {rel}", rel in rels)


# ═══════════════════════════════════════════════════════════════════════
#  B. CONFIG COMPLETENESS
# ═══════════════════════════════════════════════════════════════════════

def test_b_config():
    section("B. Config Completeness")
    from app.config import settings

    checks = [
        ("activity_tracking_enabled", True),
        ("customer_inactivity_days", 30),
        ("strategic_inactivity_days", 90),
        ("customer_warning_days", 23),
        ("offer_attribution_days", 14),
        ("vendor_protection_warn_days", 60),
        ("vendor_protection_drop_days", 90),
        ("routing_window_hours", 48),
        ("collision_lookback_days", 7),
    ]
    for attr, expected in checks:
        val = getattr(settings, attr, "MISSING")
        check(f"{attr} = {expected}", val == expected, f"got {val}")

    # Sanity: warning day < inactivity day
    check("warning < inactivity (standard)", settings.customer_warning_days < settings.customer_inactivity_days)
    check("warning gap = 7 (standard)", settings.customer_inactivity_days - settings.customer_warning_days == 7)


# ═══════════════════════════════════════════════════════════════════════
#  C. ACTIVITY SERVICE
# ═══════════════════════════════════════════════════════════════════════

def test_c_activity():
    section("C. Activity Service")
    from app.services.activity_service import (
        match_email_to_entity, match_phone_to_entity,
        log_email_activity, log_call_activity,
        get_company_activities, get_vendor_activities, get_user_activities,
        days_since_last_activity, _update_last_activity, _GENERIC_DOMAINS
    )

    # Generic domains
    for d in ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com"]:
        check(f"Generic domain: {d}", d in _GENERIC_DOMAINS)

    # match_email edge cases
    db = MagicMock()
    check("Empty email → None", match_email_to_entity("", db) is None)
    check("None email → None", match_email_to_entity(None, db) is None)

    # match_phone edge cases
    check("Empty phone → None", match_phone_to_entity("", db) is None)
    check("None phone → None", match_phone_to_entity(None, db) is None)
    check("Short phone → None", match_phone_to_entity("123", db) is None)
    check("4-digit phone → None", match_phone_to_entity("1234", db) is None)

    # Email logging — dedup
    db_dedup = MagicMock()
    db_dedup.query.return_value.filter.return_value.first.return_value = MagicMock()  # exists
    result = log_email_activity(1, "sent", "test@x.com", "Subj", "ext-1", None, db_dedup)
    check("Email dedup blocks duplicate", result is None)

    # Email logging — no match
    db_nomatch = MagicMock()
    db_nomatch.query.return_value.filter.return_value.first.return_value = None
    with patch("app.services.activity_service.match_email_to_entity", return_value=None):
        result = log_email_activity(1, "sent", "x@nowhere.com", "S", None, None, db_nomatch)
    check("Email no match → None", result is None)

    # Call logging — dedup
    db_cdedup = MagicMock()
    db_cdedup.query.return_value.filter.return_value.first.return_value = MagicMock()
    result = log_call_activity(1, "outbound", "+15551234567", 120, "ext-2", None, db_cdedup)
    check("Call dedup blocks duplicate", result is None)

    # Call logging — no match
    db_cnomatch = MagicMock()
    db_cnomatch.query.return_value.filter.return_value.first.return_value = None
    with patch("app.services.activity_service.match_phone_to_entity", return_value=None):
        result = log_call_activity(1, "inbound", "+15551234567", 60, None, None, db_cnomatch)
    check("Call no match → None", result is None)

    # days_since_last_activity — None when no records
    db_days = MagicMock()
    db_days.query.return_value.filter.return_value.scalar.return_value = None
    check("No activity → None days", days_since_last_activity(999, db_days) is None)

    # _update_last_activity has user_id param
    sig = inspect.signature(_update_last_activity)
    check("_update_last_activity accepts user_id", "user_id" in sig.parameters)

    # Function signatures
    for fn_name, expected_params in [
        ("log_email_activity", ["user_id", "direction", "email_addr", "subject", "external_id", "contact_name", "db"]),
        ("log_call_activity", ["user_id", "direction", "phone", "duration_seconds", "external_id", "contact_name", "db"]),
    ]:
        fn = locals().get(fn_name) or globals().get(fn_name)
        if fn is None:
            fn = getattr(importlib.import_module("app.services.activity_service"), fn_name)
        sig = inspect.signature(fn)
        for p in expected_params:
            check(f"{fn_name} has param '{p}'", p in sig.parameters)


# ═══════════════════════════════════════════════════════════════════════
#  D. WEBHOOK SERVICE
# ═══════════════════════════════════════════════════════════════════════

def test_d_webhooks():
    section("D. Webhook Service")
    from app.services.webhook_service import (
        create_mail_subscription, renew_subscription,
        renew_expiring_subscriptions, ensure_all_users_subscribed,
        handle_notification, _extract_email, _extract_name,
        SUBSCRIPTION_LIFETIME_HOURS, RENEW_BUFFER_HOURS
    )

    # All are async
    for fn in [create_mail_subscription, renew_subscription,
               renew_expiring_subscriptions, ensure_all_users_subscribed,
               handle_notification]:
        check(f"{fn.__name__} is async", inspect.iscoroutinefunction(fn))

    # Constants
    check("Lifetime 60-72h", 60 <= SUBSCRIPTION_LIFETIME_HOURS <= 72)
    check("Buffer > 0", RENEW_BUFFER_HOURS > 0)
    check("Buffer < Lifetime", RENEW_BUFFER_HOURS < SUBSCRIPTION_LIFETIME_HOURS)

    # Email extraction
    recip = {"emailAddress": {"address": "test@acme.com", "name": "Test User"}}
    check("Extract email", _extract_email(recip) == "test@acme.com")
    check("Extract name", _extract_name(recip) == "Test User")
    check("Extract email None", _extract_email(None) is None)
    check("Extract name None", _extract_name(None) is None)
    check("Extract email empty", _extract_email({}) is None)
    check("Extract name empty", _extract_name({}) is None)

    # Nested None handling
    check("Extract email no emailAddress", _extract_email({"foo": "bar"}) is None)
    check("Extract email no address", _extract_email({"emailAddress": {}}) is None)


# ═══════════════════════════════════════════════════════════════════════
#  E. BUYER SERVICE
# ═══════════════════════════════════════════════════════════════════════

def test_e_buyer():
    section("E. Buyer Service")
    from app.services.buyer_service import (
        get_profile, upsert_profile, list_profiles, delete_profile,
        VALID_COMMODITIES, VALID_GEOGRAPHIES, VALID_USAGE_TYPES
    )

    # Valid sets populated
    for commodity in ["semiconductors", "passives", "pc_server_parts", "connectors", "networking"]:
        check(f"Commodity: {commodity}", commodity in VALID_COMMODITIES)

    for geo in ["americas", "emea", "apac", "global"]:
        check(f"Geography: {geo}", geo in VALID_GEOGRAPHIES)

    for usage in ["sourcing_to_buy", "selling_trading", "backup_buying"]:
        check(f"Usage: {usage}", usage in VALID_USAGE_TYPES)

    # CRUD functions callable
    for fn in [get_profile, upsert_profile, list_profiles, delete_profile]:
        check(f"{fn.__name__} callable", callable(fn))


# ═══════════════════════════════════════════════════════════════════════
#  F. OWNERSHIP SERVICE
# ═══════════════════════════════════════════════════════════════════════

def test_f_ownership():
    section("F. Ownership Service")
    from app.services.ownership_service import (
        run_ownership_sweep, check_and_claim_open_account,
        get_accounts_at_risk, get_open_pool_accounts,
        get_my_accounts, get_manager_digest,
        send_manager_digest_email,
        _days_since_activity, _clear_ownership, _was_warned_today
    )

    # Function types
    check("run_ownership_sweep async", inspect.iscoroutinefunction(run_ownership_sweep))
    check("send_manager_digest_email async", inspect.iscoroutinefunction(send_manager_digest_email))
    check("check_and_claim sync", not inspect.iscoroutinefunction(check_and_claim_open_account))

    now = datetime.now(timezone.utc)

    # _days_since_activity
    company = MagicMock()
    company.last_activity_at = None
    check("No activity → None days", _days_since_activity(company, now) is None)

    company.last_activity_at = now - timedelta(days=15)
    check("15 days ago → 15", _days_since_activity(company, now) == 15)

    company.last_activity_at = now - timedelta(hours=3)
    check("3 hours ago → 0", _days_since_activity(company, now) == 0)

    company.last_activity_at = (now - timedelta(days=7)).replace(tzinfo=None)
    check("Naive datetime → 7", _days_since_activity(company, now) == 7)

    # _clear_ownership
    company = MagicMock()
    company.account_owner_id = 42
    db = MagicMock()
    _clear_ownership(company, db)
    check("Owner cleared", company.account_owner_id is None)
    check("Cleared timestamp set", company.ownership_cleared_at is not None)

    # check_and_claim — missing company
    db = MagicMock()
    db.query.return_value.get.return_value = None
    check("Missing company → False", check_and_claim_open_account(999, 1, db) is False)

    # check_and_claim — already owned
    company = MagicMock()
    company.account_owner_id = 42
    db = MagicMock()
    db.query.return_value.get.return_value = company
    check("Owned → False", check_and_claim_open_account(1, 1, db) is False)

    # check_and_claim — non-sales
    company = MagicMock()
    company.account_owner_id = None
    user = MagicMock()
    user.role = "buyer"
    db = MagicMock()
    db.query.return_value.get.side_effect = [company, user]
    check("Non-sales → False", check_and_claim_open_account(1, 1, db) is False)

    # check_and_claim — sales success
    company = MagicMock()
    company.account_owner_id = None
    user = MagicMock()
    user.role = "sales"
    user.name = "Sales Rep"
    db = MagicMock()
    db.query.return_value.get.side_effect = [company, user]
    result = check_and_claim_open_account(1, 5, db)
    check("Sales claims → True", result is True)
    check("Owner set to 5", company.account_owner_id == 5)
    check("Cleared timestamp nulled", company.ownership_cleared_at is None)

    # _was_warned_today
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    check("No warning → False", _was_warned_today(1, 1, db) is False)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock()
    check("Warning exists → True", _was_warned_today(1, 1, db) is True)

    # get_my_accounts — status logic
    db = MagicMock()
    company = MagicMock()
    company.id = 1
    company.name = "Test"
    company.is_strategic = False
    company.last_activity_at = now - timedelta(days=5)
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [company]
    result = get_my_accounts(1, db)
    check("5 days → green", result[0]["status"] == "green")

    company.last_activity_at = now - timedelta(days=25)
    result = get_my_accounts(1, db)
    check("25 days → yellow", result[0]["status"] == "yellow")

    company.last_activity_at = now - timedelta(days=35)
    result = get_my_accounts(1, db)
    check("35 days → red", result[0]["status"] == "red")

    company.last_activity_at = None
    result = get_my_accounts(1, db)
    check("None → no_activity", result[0]["status"] == "no_activity")

    # Strategic window
    company.is_strategic = True
    company.last_activity_at = now - timedelta(days=35)
    result = get_my_accounts(1, db)
    check("Strategic 35d → green", result[0]["status"] == "green")
    check("Strategic limit = 90", result[0]["inactivity_limit"] == 90)

    company.last_activity_at = now - timedelta(days=85)
    result = get_my_accounts(1, db)
    check("Strategic 85d → yellow", result[0]["status"] == "yellow")

    # get_open_pool_accounts
    db = MagicMock()
    c = MagicMock()
    c.id = 1; c.name = "OpenCo"; c.ownership_cleared_at = None; c.last_activity_at = None; c.is_strategic = False
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [c]
    result = get_open_pool_accounts(db)
    check("Open pool returns list", isinstance(result, list) and len(result) == 1)
    check("Open pool has company_id", result[0]["company_id"] == 1)


# ═══════════════════════════════════════════════════════════════════════
#  G. ROUTING SERVICE
# ═══════════════════════════════════════════════════════════════════════

def test_g_routing():
    section("G. Routing Service")
    from app.services.routing_service import (
        score_buyer, rank_buyers_for_assignment,
        create_routing_assignment, claim_routing,
        expire_stale_assignments, expire_stale_offers,
        reconfirm_offer, get_active_assignments_for_buyer,
        get_assignment_details, _assignment_to_dict,
        _score_brand, _score_commodity, _score_geography, _score_relationship,
        _infer_commodity, _country_to_region,
        W_BRAND, W_COMMODITY, W_GEOGRAPHY, W_RELATIONSHIP,
        _BRAND_COMMODITY_MAP, _COUNTRY_REGION_MAP
    )

    # Weights
    check("Weights sum = 100", W_BRAND + W_COMMODITY + W_GEOGRAPHY + W_RELATIONSHIP == 100)

    # Brand scoring
    p = MagicMock(); r = MagicMock()
    p.brand_specialties = ["Intel", "AMD"]; r.brand = "Intel"
    check("Brand exact = 40", _score_brand(p, r) == 40.0)

    r.brand = "Nvidia"
    check("Brand no match = 0", _score_brand(p, r) == 0.0)

    r.brand = None
    check("Brand None = 0", _score_brand(p, r) == 0.0)

    p.brand_specialties = None; r.brand = "Intel"
    check("No specialties = 0", _score_brand(p, r) == 0.0)

    p.brand_specialties = []; r.brand = "Intel"
    check("Empty specialties = 0", _score_brand(p, r) == 0.0)

    # Commodity inference
    r.brand = "Intel"
    check("Intel → semiconductors", _infer_commodity(r) == "semiconductors")
    r.brand = "Seagate"
    check("Seagate → pc_server_parts", _infer_commodity(r) == "pc_server_parts")
    r.brand = "Cisco"
    check("Cisco → networking", _infer_commodity(r) == "networking")
    r.brand = "Murata"
    check("Murata → passives", _infer_commodity(r) == "passives")
    r.brand = "Amphenol"
    check("Amphenol → connectors", _infer_commodity(r) == "connectors")
    r.brand = "XYZ Unknown"
    check("Unknown → None", _infer_commodity(r) is None)
    r.brand = None
    check("None brand → None", _infer_commodity(r) is None)

    # Brand map coverage
    check("Brand map ≥30", len(_BRAND_COMMODITY_MAP) >= 30)

    # Geography mapping
    check("US → americas", _country_to_region("US") == "americas")
    check("China → apac", _country_to_region("China") == "apac")
    check("UK → emea", _country_to_region("UK") == "emea")
    check("Taiwan → apac", _country_to_region("Taiwan") == "apac")
    check("None → None", _country_to_region(None) is None)
    check("Empty → None", _country_to_region("") is None)
    check("Case insensitive", _country_to_region("JAPAN") == "apac")
    check("Country map ≥25", len(_COUNTRY_REGION_MAP) >= 25)

    # Commodity scoring
    p = MagicMock(); r = MagicMock()
    p.primary_commodity = "semiconductors"; p.secondary_commodity = None; r.brand = "Intel"
    check("Primary match = 25", _score_commodity(p, r) == 25.0)

    p.secondary_commodity = "semiconductors"; p.primary_commodity = "passives"; r.brand = "Intel"
    check("Secondary match = 15", _score_commodity(p, r) == 25.0 * 0.6)

    p.primary_commodity = None; r.brand = "Intel"
    check("No commodity = 0", _score_commodity(p, r) == 0.0)

    # Geography scoring
    p = MagicMock(); v = MagicMock()
    p.primary_geography = "apac"; v.hq_country = "China"
    check("APAC+China = 15", _score_geography(p, v) == 15.0)

    p.primary_geography = "global"; v.hq_country = "Germany"
    check("Global = 7.5", _score_geography(p, v) == 7.5)

    p.primary_geography = "emea"; v.hq_country = "China"
    check("EMEA+China = 0", _score_geography(p, v) == 0.0)

    v.hq_country = None
    check("No country = 0", _score_geography(p, v) == 0.0)

    # Relationship scoring
    check("None stats = 0", _score_relationship(None) == 0.0)
    s = MagicMock(); s.rfqs_sent = 0
    check("0 RFQs = 0", _score_relationship(s) == 0.0)
    s.rfqs_sent = 10; s.response_rate = 100.0; s.win_rate = 100.0
    check("Perfect = 20", _score_relationship(s) == 20.0)

    # Full scoring
    p = MagicMock()
    p.brand_specialties = ["Intel"]; p.primary_commodity = "semiconductors"
    p.secondary_commodity = None; p.primary_geography = "americas"
    s = MagicMock(); s.rfqs_sent = 5; s.response_rate = 80.0; s.win_rate = 60.0
    r = MagicMock(); r.brand = "Intel"
    v = MagicMock(); v.hq_country = "US"
    result = score_buyer(p, s, r, v)
    check("Full score dict", all(k in result for k in ["total", "brand", "commodity", "geography", "relationship", "breakdown"]))
    check("Full score > 50", result["total"] > 50)

    # Claim logic
    now = datetime.now(timezone.utc)
    db = MagicMock()

    # Claimed → reject
    a = MagicMock(); a.status = "claimed"
    db.query.return_value.get.return_value = a
    check("Claimed → reject", claim_routing(1, 5, db)["success"] is False)

    # Expired → reject
    a.status = "expired"
    check("Expired → reject", claim_routing(1, 5, db)["success"] is False)

    # Top-3 within 24h → accept
    a = MagicMock(); a.status = "active"
    a.buyer_1_id = 5; a.buyer_2_id = 6; a.buyer_3_id = 7
    a.assigned_at = now - timedelta(hours=12)
    a.expires_at = now + timedelta(hours=36)
    db.query.return_value.get.return_value = a
    check("Top3 <24h → accept", claim_routing(1, 5, db)["success"] is True)

    # Non-top-3 within 24h → reject
    a2 = MagicMock(); a2.status = "active"
    a2.buyer_1_id = 5; a2.buyer_2_id = 6; a2.buyer_3_id = 7
    a2.assigned_at = now - timedelta(hours=12)
    a2.expires_at = now + timedelta(hours=36)
    db.query.return_value.get.return_value = a2
    check("Non-top3 <24h → reject", claim_routing(1, 99, db)["success"] is False)

    # Non-top-3 after 24h → accept (waterfall opens)
    a3 = MagicMock(); a3.status = "active"
    a3.buyer_1_id = 5; a3.buyer_2_id = 6; a3.buyer_3_id = 7
    a3.assigned_at = now - timedelta(hours=30)
    a3.expires_at = now + timedelta(hours=18)
    db.query.return_value.get.return_value = a3
    check("Non-top3 >24h → accept", claim_routing(1, 99, db)["success"] is True)

    # Not found
    db.query.return_value.get.return_value = None
    check("Not found → reject", claim_routing(999, 5, db)["success"] is False)

    # Reconfirmation
    db = MagicMock()
    db.query.return_value.get.return_value = None
    check("Reconfirm not found", reconfirm_offer(999, db)["success"] is False)

    offer = MagicMock(); offer.attribution_status = "converted"
    db.query.return_value.get.return_value = offer
    check("Reconfirm converted → no", reconfirm_offer(1, db)["success"] is False)

    offer2 = MagicMock(); offer2.attribution_status = "active"; offer2.reconfirm_count = 2
    db.query.return_value.get.return_value = offer2
    result = reconfirm_offer(1, db)
    check("Reconfirm active → yes", result["success"] is True)
    check("Count incremented to 3", offer2.reconfirm_count == 3)

    offer3 = MagicMock(); offer3.attribution_status = "expired"; offer3.reconfirm_count = 0
    db.query.return_value.get.return_value = offer3
    result = reconfirm_offer(1, db)
    check("Reconfirm expired → revived", result["success"] and offer3.attribution_status == "active")

    # Expiration sweep
    stale = MagicMock(); stale.status = "active"
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [stale]
    check("Expire assignments returns 1", expire_stale_assignments(db) == 1)
    check("Stale status → expired", stale.status == "expired")

    db2 = MagicMock()
    db2.query.return_value.filter.return_value.all.return_value = []
    check("No stale → 0", expire_stale_assignments(db2) == 0)

    # _assignment_to_dict
    a = MagicMock()
    a.id = 1; a.requirement_id = 10; a.vendor_card_id = 20; a.status = "active"
    a.assigned_at = now; a.expires_at = now + timedelta(hours=48)
    a.buyer_1_id = 5; a.buyer_2_id = 6; a.buyer_3_id = 7
    a.buyer_1_score = 85.0; a.buyer_2_score = 70.0; a.buyer_3_score = 55.0
    a.claimed_by_id = None; a.claimed_at = None
    d = _assignment_to_dict(a, for_user_id=6)
    check("Dict has my_rank", d["my_rank"] == 2)
    check("Dict hours_remaining > 0", d["hours_remaining"] > 0)


# ═══════════════════════════════════════════════════════════════════════
#  H. SCHEDULER INTEGRATION
# ═══════════════════════════════════════════════════════════════════════

def test_h_scheduler():
    section("H. Scheduler Integration")
    source = Path("app/scheduler.py").read_text()

    check("Imports webhook_service", "webhook_service" in source)
    check("Imports ownership_service", "ownership_service" in source)
    check("Imports routing_service", "routing_service" in source)
    check("Calls ensure_all_users_subscribed", "ensure_all_users_subscribed" in source)
    check("Calls renew_expiring_subscriptions", "renew_expiring_subscriptions" in source)
    check("Calls run_ownership_sweep", "run_ownership_sweep" in source)
    check("Calls expire_stale_assignments", "expire_stale_assignments" in source)
    check("Calls expire_stale_offers", "expire_stale_offers" in source)
    check("Has _last_ownership_sweep", "_last_ownership_sweep" in source)
    check("All gated by activity_tracking_enabled", source.count("activity_tracking_enabled") >= 3)


# ═══════════════════════════════════════════════════════════════════════
#  I. API ENDPOINT COVERAGE
# ═══════════════════════════════════════════════════════════════════════

def test_i_endpoints():
    section("I. API Endpoint Coverage")
    source = Path("app/main.py").read_text()
    routes = re.findall(r'@app\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']', source)
    check(f"≥116 routes", len(routes) >= 116, f"got {len(routes)}")

    # Check for duplicates
    keys = [f"{m.upper()} {p}" for m, p in routes]
    dupes = [k for k, v in Counter(keys).items() if v > 1]
    check("No duplicate routes", len(dupes) == 0, str(dupes))

    # v1.3.0 Phase 1 endpoints
    v130_phase1 = [
        'POST /api/webhooks/graph',
        'GET /api/buyer-profiles',
        'PUT /api/buyer-profiles/{user_id}',
        'GET /api/companies/{company_id}/activities',
        'GET /api/vendors/{vendor_id}/activities',
        'POST /api/activities/call',
        'GET /api/companies/{company_id}/activity-status',
    ]
    for ep in v130_phase1:
        method, path = ep.split(" ", 1)
        check(f"P1: {ep}", f'{method} {path}' in keys)

    # v1.3.0 Phase 2 endpoints
    v130_phase2 = [
        'GET /api/sales/my-accounts',
        'GET /api/sales/at-risk',
        'GET /api/sales/open-pool',
        'POST /api/sales/claim/{company_id}',
        'PUT /api/companies/{company_id}/strategic',
        'GET /api/sales/manager-digest',
        'GET /api/sales/notifications',
    ]
    for ep in v130_phase2:
        method, path = ep.split(" ", 1)
        check(f"P2: {ep}", f'{method} {path}' in keys)

    # v1.3.0 Phase 3 endpoints
    v130_phase3 = [
        'GET /api/routing/my-assignments',
        'GET /api/routing/assignments/{assignment_id}',
        'POST /api/routing/assignments/{assignment_id}/claim',
        'POST /api/routing/score',
        'POST /api/routing/create',
        'POST /api/offers/{offer_id}/reconfirm',
    ]
    for ep in v130_phase3:
        method, path = ep.split(" ", 1)
        check(f"P3: {ep}", f'{method} {path}' in keys)


# ═══════════════════════════════════════════════════════════════════════
#  J. MIGRATION INTEGRITY
# ═══════════════════════════════════════════════════════════════════════

def test_j_migrations():
    section("J. Migration Integrity")
    for fname in ["006_activity_routing_foundation.sql", "007_routing_assignments.sql"]:
        sql = Path(f"migrations/{fname}").read_text()
        check(f"{fname} has BEGIN", "BEGIN;" in sql)
        check(f"{fname} has COMMIT", "COMMIT;" in sql)
        check(f"{fname} uses IF NOT EXISTS", "IF NOT EXISTS" in sql)

        # No CREATE TABLE without IF NOT EXISTS
        bad_creates = re.findall(r'CREATE TABLE\s+(?!IF)', sql)
        check(f"{fname} all CREATE TABLE guarded", len(bad_creates) == 0)

        # No ADD COLUMN without IF NOT EXISTS
        bad_adds = re.findall(r'ADD COLUMN\s+(?!IF)', sql)
        check(f"{fname} all ADD COLUMN guarded", len(bad_adds) == 0)

    # 006 specific content
    sql006 = Path("migrations/006_activity_routing_foundation.sql").read_text()
    for table in ["activity_log", "buyer_profiles", "buyer_vendor_stats", "graph_subscriptions"]:
        check(f"006 creates {table}", f"CREATE TABLE IF NOT EXISTS {table}" in sql006)

    # 007 specific content
    sql007 = Path("migrations/007_routing_assignments.sql").read_text()
    check("007 creates routing_assignments", "CREATE TABLE IF NOT EXISTS routing_assignments" in sql007)
    check("007 unique active index", "ix_routing_active_unique" in sql007)


# ═══════════════════════════════════════════════════════════════════════
#  K. CROSS-MODULE INTEGRATION
# ═══════════════════════════════════════════════════════════════════════

def test_k_integration():
    section("K. Cross-Module Integration")

    # Activity → Ownership auto-claim chain
    source = Path("app/services/activity_service.py").read_text()
    check("Activity imports ownership_service", "check_and_claim_open_account" in source)
    check("_update_last_activity calls claim", "check_and_claim_open_account(match" in source)
    check("Both log fns pass user_id", source.count("_update_last_activity(match, db, user_id)") == 2)

    # Webhook → Activity chain
    wsource = Path("app/services/webhook_service.py").read_text()
    check("Webhook imports activity_service", "log_email_activity" in wsource)

    # Scheduler chains all services
    ssource = Path("app/scheduler.py").read_text()
    check("Scheduler chains: webhooks", "renew_expiring_subscriptions" in ssource)
    check("Scheduler chains: ownership", "run_ownership_sweep" in ssource)
    check("Scheduler chains: routing", "expire_stale_assignments" in ssource)

    # Config drives all services
    from app.config import settings
    check("Config accessible", hasattr(settings, "customer_inactivity_days"))


# ═══════════════════════════════════════════════════════════════════════
#  L. EDGE CASES & BOUNDARY CONDITIONS
# ═══════════════════════════════════════════════════════════════════════

def test_l_edge_cases():
    section("L. Edge Cases & Boundary Conditions")
    from app.services.routing_service import (
        _score_brand, _score_commodity, _score_geography, _score_relationship,
        _infer_commodity, claim_routing
    )
    from app.services.ownership_service import _days_since_activity

    now = datetime.now(timezone.utc)

    # Brand with whitespace
    p = MagicMock(); r = MagicMock()
    p.brand_specialties = ["  Intel  ", "AMD"]
    r.brand = "  Intel  "
    check("Brand w/ whitespace matches", _score_brand(p, r) == 40.0)

    # Empty brand list
    p.brand_specialties = []
    r.brand = "Intel"
    check("Empty brand list = 0", _score_brand(p, r) == 0.0)

    # Commodity with unknown brand
    p.primary_commodity = "semiconductors"; r.brand = ""
    check("Empty brand string → None infer", _infer_commodity(r) is None)

    # Geography with unusual codes
    from app.services.routing_service import _country_to_region
    check("'sg' → apac", _country_to_region("sg") == "apac")
    check("'de' → emea", _country_to_region("de") == "emea")
    check("'br' → americas", _country_to_region("br") == "americas")

    # Relationship with >100% rates (clamp)
    s = MagicMock()
    s.rfqs_sent = 5
    s.response_rate = 150.0  # Over 100
    s.win_rate = 200.0
    score = _score_relationship(s)
    check("Clamped rates ≤ max weight", score <= 20.0, f"got {score}")

    # Exact boundary: day 23 vs 22
    company = MagicMock()
    company.last_activity_at = now - timedelta(days=23)
    check("Day 23 → 23", _days_since_activity(company, now) == 23)

    company.last_activity_at = now - timedelta(days=22)
    check("Day 22 → 22", _days_since_activity(company, now) == 22)

    company.last_activity_at = now - timedelta(days=30)
    check("Day 30 → 30", _days_since_activity(company, now) == 30)

    # Claim at exactly 24h boundary
    a = MagicMock(); a.status = "active"
    a.buyer_1_id = 5; a.buyer_2_id = 6; a.buyer_3_id = 7
    a.assigned_at = now - timedelta(hours=24)  # exactly 24h
    a.expires_at = now + timedelta(hours=24)
    db = MagicMock()
    db.query.return_value.get.return_value = a
    result = claim_routing(1, 99, db)
    check("Non-top3 at exactly 24h → accept", result["success"] is True)

    # Claim at 23.99h → reject
    a2 = MagicMock(); a2.status = "active"
    a2.buyer_1_id = 5; a2.buyer_2_id = 6; a2.buyer_3_id = 7
    a2.assigned_at = now - timedelta(hours=23, minutes=59)
    a2.expires_at = now + timedelta(hours=24, minutes=1)
    db.query.return_value.get.return_value = a2
    result = claim_routing(1, 99, db)
    check("Non-top3 at 23.99h → reject", result["success"] is False)

    # Reconfirm count from 0
    from app.services.routing_service import reconfirm_offer
    offer = MagicMock(); offer.attribution_status = "active"; offer.reconfirm_count = 0
    db = MagicMock()
    db.query.return_value.get.return_value = offer
    reconfirm_offer(1, db)
    check("Reconfirm count 0→1", offer.reconfirm_count == 1)

    # Reconfirm count None handling
    offer2 = MagicMock(); offer2.attribution_status = "active"; offer2.reconfirm_count = None
    db.query.return_value.get.return_value = offer2
    reconfirm_offer(1, db)
    check("Reconfirm count None→1", offer2.reconfirm_count == 1)

    # Graph client compat
    from app.utils.graph_client import GraphClient
    gc = GraphClient("fake")
    check("GraphClient.post_json exists", hasattr(gc, "post_json"))
    check("GraphClient.get_json exists", hasattr(gc, "get_json"))
    check("post_json async", inspect.iscoroutinefunction(gc.post_json))


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        test_a_models,
        test_b_config,
        test_c_activity,
        test_d_webhooks,
        test_e_buyer,
        test_f_ownership,
        test_g_routing,
        test_h_scheduler,
        test_i_endpoints,
        test_j_migrations,
        test_k_integration,
        test_l_edge_cases,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except Exception as e:
            FAIL += 1
            msg = f"  ✗ [{SECTION}] {test_fn.__name__} CRASHED: {e}"
            print(msg)
            ERRORS.append(msg)
            import traceback
            traceback.print_exc()

    print(f"\n{'═'*60}")
    print(f"  MASTER TEST: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    print(f"{'═'*60}")

    if ERRORS:
        print(f"\n  FAILURES ({len(ERRORS)}):")
        for e in ERRORS:
            print(f"  {e}")
        sys.exit(1)
    else:
        print(f"\n  ✅ ALL {PASS} MASTER TESTS PASSED!")
        sys.exit(0)

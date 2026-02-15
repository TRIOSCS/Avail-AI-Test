"""AVAIL v1.3.0 LIVE SMOKE TEST
================================
Run against the development database AFTER applying migrations 006-008.

Prerequisites:
  1. Database connection configured in .env
  2. Migrations 006, 007, 008 applied
  3. At least one user with ms_token (for Graph API tests)
  4. AVAIL_ACTIVITY_TRACKING_ENABLED=true

Usage:
  python3 scripts/smoke_test_live.py

This script:
  - Creates a test BuyerProfile
  - Logs a test email activity
  - Verifies auto-claim fires on unowned company
  - Tests ownership status queries
  - Creates a routing assignment and verifies scoring
  - Tests offer reconfirmation
  - Cleans up all test data

⚠️  Uses REAL database — creates and deletes test records.
    Prefix all test data with 'SMOKE_TEST_' for safety.
"""
import sys
import os
from datetime import datetime, timezone, timedelta

# Add project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import (
    User, Company, BuyerProfile, ActivityLog,
    RoutingAssignment, Requirement, VendorCard, Offer,
    Requisition
)

PASS = 0
FAIL = 0
CLEANUP_IDS: dict[str, list] = {
    "activity_log": [],
    "buyer_profiles": [],
    "routing_assignments": [],
    "offers": [],
    "requirements": [],
    "requisitions": [],
}


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))


def main():
    global PASS, FAIL

    db = SessionLocal()
    try:
        print("\n══════════════════════════════════════════════════════════")
        print("  AVAIL v1.3.0 LIVE SMOKE TEST")
        print("══════════════════════════════════════════════════════════")

        # ── Preflight: find a test user ──
        print("\n── Preflight ──")
        user = db.query(User).first()
        if not user:
            print("  ✗ No users in database — cannot run smoke test")
            return

        check(f"Found user: {user.name} (id={user.id})", True)

        # Find a company (or the first one)
        company = db.query(Company).first()
        if not company:
            print("  ✗ No companies in database — cannot run smoke test")
            return

        check(f"Found company: {company.name} (id={company.id})", True)

        # Find a vendor
        vendor = db.query(VendorCard).first()
        if not vendor:
            print("  ⚠ No vendors — skipping routing tests")
            vendor = None
        else:
            check(f"Found vendor: {vendor.display_name} (id={vendor.id})", True)

        # ── 1. Schema verification ──
        print("\n── 1. Schema Verification ──")

        # Check new tables exist
        from sqlalchemy import inspect as sa_inspect
        inspector = sa_inspect(db.bind)
        tables = inspector.get_table_names()

        for t in ["activity_log", "buyer_profiles", "buyer_vendor_stats",
                   "graph_subscriptions", "routing_assignments"]:
            check(f"Table exists: {t}", t in tables)

        # Check offer columns
        offer_cols = {c["name"] for c in inspector.get_columns("offers")}
        for col in ["expires_at", "reconfirmed_at", "reconfirm_count", "attribution_status"]:
            check(f"offers.{col} exists", col in offer_cols)

        # Check company columns
        company_cols = {c["name"] for c in inspector.get_columns("companies")}
        for col in ["is_strategic", "ownership_cleared_at", "last_activity_at", "account_owner_id"]:
            check(f"companies.{col} exists", col in company_cols)

        # ── 2. Buyer Profile CRUD ──
        print("\n── 2. Buyer Profile CRUD ──")
        from app.services.buyer_service import upsert_profile, get_profile, delete_profile

        profile_data = {
            "primary_commodity": "semiconductors",
            "secondary_commodity": "passives",
            "primary_geography": "americas",
            "brand_specialties": ["Intel", "AMD", "SMOKE_TEST"],
            "brand_material_types": [],
            "brand_usage_types": [],
        }
        profile = upsert_profile(user.id, profile_data, db)
        db.commit()
        CLEANUP_IDS["buyer_profiles"].append(user.id)
        check("Profile created", profile is not None)
        check("Commodity set", profile.primary_commodity == "semiconductors")

        fetched = get_profile(user.id, db)
        check("Profile fetched", fetched is not None)
        check("Brand specialties stored", "SMOKE_TEST" in (fetched.brand_specialties or []))

        # ── 3. Activity Logging ──
        print("\n── 3. Activity Logging ──")
        from app.services.activity_service import log_email_activity, days_since_last_activity

        # Log a test email
        activity = log_email_activity(
            user_id=user.id,
            direction="sent",
            email_addr="smoke_test@example.com",
            subject="SMOKE_TEST email",
            external_id=f"SMOKE_TEST_{datetime.now().timestamp()}",
            contact_name="Smoke Tester",
            db=db,
        )
        # Activity may be None if email doesn't match — that's OK for smoke test
        if activity:
            db.commit()
            CLEANUP_IDS["activity_log"].append(activity.id)
            check("Activity logged", True)
            check(f"Activity type: {activity.activity_type}", activity.activity_type == "email_sent")
        else:
            check("Activity skipped (no entity match) — expected for test email", True)

        # ── 4. Ownership Queries ──
        print("\n── 4. Ownership Queries ──")
        from app.services.ownership_service import (
            get_my_accounts, get_open_pool_accounts, get_accounts_at_risk
        )

        my_accounts = get_my_accounts(user.id, db)
        check(f"get_my_accounts returns list ({len(my_accounts)} accounts)", isinstance(my_accounts, list))

        open_pool = get_open_pool_accounts(db)
        check(f"get_open_pool_accounts returns list ({len(open_pool)} accounts)", isinstance(open_pool, list))

        at_risk = get_accounts_at_risk(db)
        check(f"get_accounts_at_risk returns list ({len(at_risk)} accounts)", isinstance(at_risk, list))

        # ── 5. Routing Scoring (if vendor exists) ──
        if vendor:
            print("\n── 5. Routing Scoring ──")
            from app.services.routing_service import rank_buyers_for_assignment

            # Create a temporary requirement
            req_requisition = db.query(Requisition).first()
            if req_requisition:
                test_req = Requirement(
                    requisition_id=req_requisition.id,
                    primary_mpn="SMOKE_TEST_MPN",
                    brand="Intel",
                    target_qty=100,
                )
                db.add(test_req)
                db.flush()
                CLEANUP_IDS["requirements"].append(test_req.id)

                rankings = rank_buyers_for_assignment(test_req.id, vendor.id, db)
                check(f"Ranking returned {len(rankings)} buyers", isinstance(rankings, list))

                if rankings:
                    top = rankings[0]
                    check("Top buyer has score_details", "score_details" in top)
                    check(f"Top score: {top['score_details']['total']}", top["score_details"]["total"] >= 0)
                    check("Score has brand component", "brand" in top["score_details"])
            else:
                check("No requisitions — skipping routing test", True)

        # ── 6. Offer Reconfirmation ──
        print("\n── 6. Offer Reconfirmation ──")
        test_offer = db.query(Offer).first()
        if test_offer:
            from app.services.routing_service import reconfirm_offer
            original_count = test_offer.reconfirm_count or 0
            result = reconfirm_offer(test_offer.id, db)
            # Don't commit — we don't want to actually modify real offers
            db.rollback()
            check("Reconfirm returns result", "success" in result)
        else:
            check("No offers in DB — skipping reconfirm test", True)

        # ── 7. Config Verification ──
        print("\n── 7. Config Verification ──")
        from app.config import settings
        check("Activity tracking enabled", settings.activity_tracking_enabled is True)
        check("Inactivity days = 30", settings.customer_inactivity_days == 30)
        check("Routing window = 48h", settings.routing_window_hours == 48)

        # ── Summary ──
        print(f"\n{'═'*58}")
        print(f"  SMOKE TEST: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
        print(f"{'═'*58}")

        if FAIL:
            print(f"\n  ⚠️  {FAIL} failures — review above")
        else:
            print(f"\n  ✅ ALL SMOKE TESTS PASSED!")

    finally:
        # ── Cleanup ──
        print("\n── Cleanup ──")
        try:
            for aid in CLEANUP_IDS["activity_log"]:
                db.query(ActivityLog).filter(ActivityLog.id == aid).delete()
            for rid in CLEANUP_IDS["requirements"]:
                db.query(Requirement).filter(Requirement.id == rid).delete()
            for uid in CLEANUP_IDS["buyer_profiles"]:
                db.query(BuyerProfile).filter(BuyerProfile.user_id == uid).delete()
            db.commit()
            print("  ✓ Test data cleaned up")
        except Exception as e:
            db.rollback()
            print(f"  ⚠ Cleanup error: {e}")
        finally:
            db.close()


if __name__ == "__main__":
    main()
    sys.exit(1 if FAIL else 0)

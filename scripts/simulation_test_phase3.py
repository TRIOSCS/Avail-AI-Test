"""Simulation tests for AVAIL v1.3.0 Phase 3 — Buyer Routing.

Tests: expertise scoring, brand/commodity/geography/relationship scoring,
       48-hour waterfall, claim logic, offer expiration, reconfirmation.

Run: python3 scripts/simulation_test_phase3.py
"""
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
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
#  Category 1: Model Definition
# ═══════════════════════════════════════════════════════════════════════

def test_model():
    print("\n── 1. RoutingAssignment Model ──")
    from app.models import RoutingAssignment

    cols = {c.name for c in RoutingAssignment.__table__.columns}
    check("Has requirement_id", "requirement_id" in cols)
    check("Has vendor_card_id", "vendor_card_id" in cols)
    check("Has buyer_1_id", "buyer_1_id" in cols)
    check("Has buyer_2_id", "buyer_2_id" in cols)
    check("Has buyer_3_id", "buyer_3_id" in cols)
    check("Has buyer_1_score", "buyer_1_score" in cols)
    check("Has scoring_details", "scoring_details" in cols)
    check("Has assigned_at", "assigned_at" in cols)
    check("Has expires_at", "expires_at" in cols)
    check("Has claimed_by_id", "claimed_by_id" in cols)
    check("Has claimed_at", "claimed_at" in cols)
    check("Has status", "status" in cols)


# ═══════════════════════════════════════════════════════════════════════
#  Category 2: Scoring Weights
# ═══════════════════════════════════════════════════════════════════════

def test_scoring_weights():
    print("\n── 2. Scoring Weights ──")
    from app.services.routing_service import W_BRAND, W_COMMODITY, W_GEOGRAPHY, W_RELATIONSHIP

    total = W_BRAND + W_COMMODITY + W_GEOGRAPHY + W_RELATIONSHIP
    check("Weights sum to 100", total == 100, f"got {total}")
    check("Brand weight = 40", W_BRAND == 40)
    check("Commodity weight = 25", W_COMMODITY == 25)
    check("Geography weight = 15", W_GEOGRAPHY == 15)
    check("Relationship weight = 20", W_RELATIONSHIP == 20)


# ═══════════════════════════════════════════════════════════════════════
#  Category 3: Brand Scoring
# ═══════════════════════════════════════════════════════════════════════

def test_brand_scoring():
    print("\n── 3. Brand Scoring ──")
    from app.services.routing_service import _score_brand, W_BRAND

    profile = MagicMock()
    requirement = MagicMock()

    # Exact match
    profile.brand_specialties = ["Texas Instruments", "Intel", "AMD"]
    requirement.brand = "Texas Instruments"
    score = _score_brand(profile, requirement)
    check("Exact brand match = full weight", score == W_BRAND, f"got {score}")

    # Case insensitive
    requirement.brand = "texas instruments"
    score = _score_brand(profile, requirement)
    check("Case insensitive match", score == W_BRAND, f"got {score}")

    # Partial match (contains)
    requirement.brand = "TI"
    profile.brand_specialties = ["Texas Instruments/TI"]
    score = _score_brand(profile, requirement)
    check("Partial match = half weight", score == W_BRAND * 0.5, f"got {score}")

    # No match
    requirement.brand = "Qualcomm"
    profile.brand_specialties = ["Intel", "AMD"]
    score = _score_brand(profile, requirement)
    check("No brand match = 0", score == 0.0, f"got {score}")

    # No brand on requirement
    requirement.brand = None
    score = _score_brand(profile, requirement)
    check("No requirement brand = 0", score == 0.0, f"got {score}")

    # No specialties
    requirement.brand = "Intel"
    profile.brand_specialties = None
    score = _score_brand(profile, requirement)
    check("No buyer specialties = 0", score == 0.0, f"got {score}")


# ═══════════════════════════════════════════════════════════════════════
#  Category 4: Commodity Scoring
# ═══════════════════════════════════════════════════════════════════════

def test_commodity_scoring():
    print("\n── 4. Commodity Scoring ──")
    from app.services.routing_service import _score_commodity, W_COMMODITY

    profile = MagicMock()
    requirement = MagicMock()

    # Primary match via brand inference
    profile.primary_commodity = "semiconductors"
    profile.secondary_commodity = "passives"
    requirement.brand = "Intel"
    score = _score_commodity(profile, requirement)
    check("Primary commodity match = full weight", score == W_COMMODITY, f"got {score}")

    # Secondary match
    profile.primary_commodity = "networking"
    profile.secondary_commodity = "semiconductors"
    requirement.brand = "AMD"
    score = _score_commodity(profile, requirement)
    check("Secondary commodity match = 60%", score == W_COMMODITY * 0.6, f"got {score}")

    # No commodity match
    profile.primary_commodity = "passives"
    profile.secondary_commodity = "connectors"
    requirement.brand = "Intel"
    score = _score_commodity(profile, requirement)
    check("No commodity match = 0", score == 0.0, f"got {score}")

    # Unknown brand → partial baseline
    requirement.brand = "Unknown Brand XYZ"
    score = _score_commodity(profile, requirement)
    check("Unknown brand gives baseline", score == W_COMMODITY * 0.25, f"got {score}")

    # No commodity on buyer
    profile.primary_commodity = None
    requirement.brand = "Intel"
    score = _score_commodity(profile, requirement)
    check("No buyer commodity = 0", score == 0.0, f"got {score}")


# ═══════════════════════════════════════════════════════════════════════
#  Category 5: Geography Scoring
# ═══════════════════════════════════════════════════════════════════════

def test_geography_scoring():
    print("\n── 5. Geography Scoring ──")
    from app.services.routing_service import _score_geography, W_GEOGRAPHY

    profile = MagicMock()
    vendor = MagicMock()

    # Exact region match
    profile.primary_geography = "apac"
    vendor.hq_country = "China"
    score = _score_geography(profile, vendor)
    check("APAC buyer + China vendor = full", score == W_GEOGRAPHY, f"got {score}")

    # Americas match
    profile.primary_geography = "americas"
    vendor.hq_country = "US"
    score = _score_geography(profile, vendor)
    check("Americas buyer + US vendor = full", score == W_GEOGRAPHY, f"got {score}")

    # Global gets half credit
    profile.primary_geography = "global"
    vendor.hq_country = "Germany"
    score = _score_geography(profile, vendor)
    check("Global buyer = half weight", score == W_GEOGRAPHY * 0.5, f"got {score}")

    # No match
    profile.primary_geography = "emea"
    vendor.hq_country = "China"
    score = _score_geography(profile, vendor)
    check("EMEA buyer + China vendor = 0", score == 0.0, f"got {score}")

    # No country on vendor
    vendor.hq_country = None
    score = _score_geography(profile, vendor)
    check("No vendor country = 0", score == 0.0, f"got {score}")


# ═══════════════════════════════════════════════════════════════════════
#  Category 6: Relationship Scoring
# ═══════════════════════════════════════════════════════════════════════

def test_relationship_scoring():
    print("\n── 6. Relationship Scoring ──")
    from app.services.routing_service import _score_relationship, W_RELATIONSHIP

    # No stats → 0
    score = _score_relationship(None)
    check("No stats = 0", score == 0.0, f"got {score}")

    # Zero RFQs → 0
    stats = MagicMock()
    stats.rfqs_sent = 0
    score = _score_relationship(stats)
    check("Zero RFQs = 0", score == 0.0, f"got {score}")

    # Perfect stats: 100% response, 100% win
    stats.rfqs_sent = 10
    stats.response_rate = 100.0
    stats.win_rate = 100.0
    score = _score_relationship(stats)
    check("Perfect stats = full weight", score == W_RELATIONSHIP, f"got {score}")

    # 50% response, 50% win
    stats.response_rate = 50.0
    stats.win_rate = 50.0
    expected = W_RELATIONSHIP * (0.5 * 0.6 + 0.5 * 0.4)  # 10.0
    score = _score_relationship(stats)
    check("50/50 stats = half weight", abs(score - expected) < 0.01, f"got {score} expected {expected}")


# ═══════════════════════════════════════════════════════════════════════
#  Category 7: Full Buyer Scoring
# ═══════════════════════════════════════════════════════════════════════

def test_full_scoring():
    print("\n── 7. Full Buyer Scoring ──")
    from app.services.routing_service import score_buyer

    profile = MagicMock()
    profile.brand_specialties = ["Intel", "AMD"]
    profile.primary_commodity = "semiconductors"
    profile.secondary_commodity = None
    profile.primary_geography = "americas"

    stats = MagicMock()
    stats.rfqs_sent = 5
    stats.response_rate = 80.0
    stats.win_rate = 60.0

    requirement = MagicMock()
    requirement.brand = "Intel"

    vendor = MagicMock()
    vendor.hq_country = "US"

    result = score_buyer(profile, stats, requirement, vendor)

    check("Returns dict with total", "total" in result)
    check("Returns brand score", "brand" in result)
    check("Returns commodity score", "commodity" in result)
    check("Returns geography score", "geography" in result)
    check("Returns relationship score", "relationship" in result)
    check("Returns breakdown", "breakdown" in result)
    check("Total > 0 for good match", result["total"] > 50, f"got {result['total']}")
    check("Brand match in breakdown", result["breakdown"]["brand_match"] is True)
    check("Has history in breakdown", result["breakdown"]["has_history"] is True)


# ═══════════════════════════════════════════════════════════════════════
#  Category 8: Commodity Inference
# ═══════════════════════════════════════════════════════════════════════

def test_commodity_inference():
    print("\n── 8. Commodity Inference ──")
    from app.services.routing_service import _infer_commodity, _BRAND_COMMODITY_MAP

    req = MagicMock()

    # Known brands
    req.brand = "Intel"
    check("Intel → semiconductors", _infer_commodity(req) == "semiconductors")

    req.brand = "Seagate"
    check("Seagate → pc_server_parts", _infer_commodity(req) == "pc_server_parts")

    req.brand = "Cisco"
    check("Cisco → networking", _infer_commodity(req) == "networking")

    req.brand = "Murata"
    check("Murata → passives", _infer_commodity(req) == "passives")

    req.brand = "Amphenol"
    check("Amphenol → connectors", _infer_commodity(req) == "connectors")

    # Unknown brand
    req.brand = "Acme Unknown Corp"
    check("Unknown brand → None", _infer_commodity(req) is None)

    # Map has reasonable coverage
    check("Brand map has 30+ entries", len(_BRAND_COMMODITY_MAP) >= 30)


# ═══════════════════════════════════════════════════════════════════════
#  Category 9: Geography Mapping
# ═══════════════════════════════════════════════════════════════════════

def test_geography_mapping():
    print("\n── 9. Geography Mapping ──")
    from app.services.routing_service import _country_to_region

    check("US → americas", _country_to_region("US") == "americas")
    check("China → apac", _country_to_region("China") == "apac")
    check("UK → emea", _country_to_region("UK") == "emea")
    check("Taiwan → apac", _country_to_region("Taiwan") == "apac")
    check("Germany → emea", _country_to_region("Germany") == "emea")
    check("Case insensitive", _country_to_region("CHINA") == "apac")
    check("None → None", _country_to_region(None) is None)
    check("Empty → None", _country_to_region("") is None)


# ═══════════════════════════════════════════════════════════════════════
#  Category 10: Claim Logic — Waterfall Rules
# ═══════════════════════════════════════════════════════════════════════

def test_claim_logic():
    print("\n── 10. Claim Logic ──")
    from app.services.routing_service import claim_routing

    db = MagicMock()
    now = datetime.now(timezone.utc)

    # Already claimed → rejected
    assignment = MagicMock()
    assignment.status = "claimed"
    db.query.return_value.get.return_value = assignment
    result = claim_routing(1, 5, db)
    check("Already claimed → rejected", result["success"] is False)

    # Expired → rejected
    assignment.status = "expired"
    result = claim_routing(1, 5, db)
    check("Expired → rejected", result["success"] is False)

    # Active, top-3 buyer within 24h → accepted
    assignment.status = "active"
    assignment.buyer_1_id = 5
    assignment.buyer_2_id = 6
    assignment.buyer_3_id = 7
    assignment.assigned_at = now - timedelta(hours=12)
    assignment.expires_at = now + timedelta(hours=36)
    result = claim_routing(1, 5, db)
    check("Top-3 buyer within 24h → accepted", result["success"] is True)
    check("claimed_by_id set", assignment.claimed_by_id == 5)
    check("Status = claimed", assignment.status == "claimed")

    # Non-top-3 within 24h → rejected
    assignment2 = MagicMock()
    assignment2.status = "active"
    assignment2.buyer_1_id = 5
    assignment2.buyer_2_id = 6
    assignment2.buyer_3_id = 7
    assignment2.assigned_at = now - timedelta(hours=12)
    assignment2.expires_at = now + timedelta(hours=36)
    db.query.return_value.get.return_value = assignment2
    result = claim_routing(1, 99, db)
    check("Non-top-3 within 24h → rejected", result["success"] is False)

    # Non-top-3 after 24h → accepted (waterfall opens)
    assignment3 = MagicMock()
    assignment3.status = "active"
    assignment3.buyer_1_id = 5
    assignment3.buyer_2_id = 6
    assignment3.buyer_3_id = 7
    assignment3.assigned_at = now - timedelta(hours=30)
    assignment3.expires_at = now + timedelta(hours=18)
    db.query.return_value.get.return_value = assignment3
    result = claim_routing(1, 99, db)
    check("Non-top-3 after 24h → accepted", result["success"] is True)


# ═══════════════════════════════════════════════════════════════════════
#  Category 11: Offer Reconfirmation
# ═══════════════════════════════════════════════════════════════════════

def test_reconfirmation():
    print("\n── 11. Offer Reconfirmation ──")
    from app.services.routing_service import reconfirm_offer

    db = MagicMock()

    # Not found
    db.query.return_value.get.return_value = None
    result = reconfirm_offer(999, db)
    check("Not found → failure", result["success"] is False)

    # Already converted
    offer = MagicMock()
    offer.attribution_status = "converted"
    db.query.return_value.get.return_value = offer
    result = reconfirm_offer(1, db)
    check("Converted → no reconfirm needed", result["success"] is False)

    # Active offer → reconfirm
    offer2 = MagicMock()
    offer2.attribution_status = "active"
    offer2.reconfirm_count = 1
    db.query.return_value.get.return_value = offer2
    result = reconfirm_offer(1, db)
    check("Active offer → reconfirmed", result["success"] is True)
    check("Reconfirm count incremented", offer2.reconfirm_count == 2)
    check("New expires_at set", offer2.expires_at is not None)
    check("Status stays active", offer2.attribution_status == "active")

    # Expired offer → reconfirm revives it
    offer3 = MagicMock()
    offer3.attribution_status = "expired"
    offer3.reconfirm_count = 0
    db.query.return_value.get.return_value = offer3
    result = reconfirm_offer(1, db)
    check("Expired offer can be reconfirmed", result["success"] is True)
    check("Status set back to active", offer3.attribution_status == "active")


# ═══════════════════════════════════════════════════════════════════════
#  Category 12: Expiration Sweeps
# ═══════════════════════════════════════════════════════════════════════

def test_expiration_sweeps():
    print("\n── 12. Expiration Sweeps ──")
    from app.services.routing_service import expire_stale_assignments, expire_stale_offers

    check("expire_stale_assignments callable", callable(expire_stale_assignments))
    check("expire_stale_offers callable", callable(expire_stale_offers))

    # Test stale assignment expiration
    now = datetime.now(timezone.utc)
    stale = MagicMock()
    stale.status = "active"
    stale.expires_at = now - timedelta(hours=1)

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [stale]
    count = expire_stale_assignments(db)
    check("Stale assignment expired", stale.status == "expired")
    check("Returns count = 1", count == 1)

    # No stale
    db2 = MagicMock()
    db2.query.return_value.filter.return_value.all.return_value = []
    count = expire_stale_assignments(db2)
    check("No stale returns 0", count == 0)


# ═══════════════════════════════════════════════════════════════════════
#  Category 13: Migration SQL
# ═══════════════════════════════════════════════════════════════════════

def test_migration():
    print("\n── 13. Migration SQL ──")
    sql = Path("migrations/007_routing_assignments.sql").read_text()

    check("Creates routing_assignments table", "CREATE TABLE IF NOT EXISTS routing_assignments" in sql)
    check("Has buyer slots (buyer_1_id)", "buyer_1_id" in sql)
    check("Has scoring_details JSON", "scoring_details" in sql)
    check("Has expires_at column", "expires_at" in sql)
    check("Has claimed_by_id", "claimed_by_id" in sql)
    check("Has unique active index", "ix_routing_active_unique" in sql)
    check("Uses BEGIN/COMMIT", "BEGIN;" in sql and "COMMIT;" in sql)


# ═══════════════════════════════════════════════════════════════════════
#  Category 14: API Endpoints
# ═══════════════════════════════════════════════════════════════════════

def test_api_endpoints():
    print("\n── 14. API Endpoints ──")
    source = Path("app/main.py").read_text()

    check("GET /api/routing/my-assignments", '"/api/routing/my-assignments"' in source)
    check("GET /api/routing/assignments/{id}", '"/api/routing/assignments/{assignment_id}"' in source)
    check("POST /api/routing/assignments/{id}/claim", '"/api/routing/assignments/{assignment_id}/claim"' in source)
    check("POST /api/routing/score", '"/api/routing/score"' in source)
    check("POST /api/routing/create", '"/api/routing/create"' in source)
    check("POST /api/offers/{id}/reconfirm", '"/api/offers/{offer_id}/reconfirm"' in source)


# ═══════════════════════════════════════════════════════════════════════
#  Category 15: Scheduler Integration
# ═══════════════════════════════════════════════════════════════════════

def test_scheduler():
    print("\n── 15. Scheduler Integration ──")
    source = Path("app/scheduler.py").read_text()

    check("Imports expire_stale_assignments", "expire_stale_assignments" in source)
    check("Imports expire_stale_offers", "expire_stale_offers" in source)
    check("Routing expiration block exists", "Routing expiration" in source)


# ═══════════════════════════════════════════════════════════════════════
#  Category 16: Config Integration
# ═══════════════════════════════════════════════════════════════════════

def test_config():
    print("\n── 16. Config Integration ──")
    from app.config import settings

    check("routing_window_hours = 48", settings.routing_window_hours == 48)
    check("offer_attribution_days = 14", settings.offer_attribution_days == 14)
    check("collision_lookback_days = 7", settings.collision_lookback_days == 7)


# ═══════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        test_model,
        test_scoring_weights,
        test_brand_scoring,
        test_commodity_scoring,
        test_geography_scoring,
        test_relationship_scoring,
        test_full_scoring,
        test_commodity_inference,
        test_geography_mapping,
        test_claim_logic,
        test_reconfirmation,
        test_expiration_sweeps,
        test_migration,
        test_api_endpoints,
        test_scheduler,
        test_config,
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
    print(f"Phase 3 Results: {PASS} passed, {FAIL} failed out of {PASS + FAIL}")
    print(f"{'='*60}")

    if ERRORS:
        print("\nFailures:")
        for e in ERRORS:
            print(e)
        sys.exit(1)
    else:
        print("\n✅ All Phase 3 tests passed!")
        sys.exit(0)

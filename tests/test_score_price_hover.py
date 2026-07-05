"""Tests for the Score/Price hover layer (idea C).

Covers the deterministic breakdown helpers (their contributions reconcile to the score
they explain — the anti-drift guarantee), the price-series helper, the shared hover
macro's three variants, and a representative column rendering the hover wired to real
data with NO AI call involved.

Called by: pytest.
Depends on: app.scoring, app.services.vendor_score, app.services.prospect_scoring,
            app.services.part_history_service, app.template_env, models.
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone

from app.models.intelligence import MaterialCard
from app.models.price_snapshot import MaterialPriceSnapshot
from app.models.prospect_account import ProspectAccount
from app.models.vendors import VendorCard
from app.scoring import score_sighting_v2, score_sighting_v2_breakdown
from app.services.part_history_service import price_series_for_card
from app.services.prospect_scoring import (
    calculate_fit_breakdown,
    calculate_fit_score,
    calculate_readiness_breakdown,
    calculate_readiness_score,
    fit_breakdown_for_prospect,
    readiness_breakdown_for_prospect,
)
from app.services.vendor_score import (
    MIN_OFFERS_FOR_SCORE,
    compute_single_vendor_score,
    compute_single_vendor_score_breakdown,
    compute_vendor_score,
    compute_vendor_score_breakdown,
)
from app.template_env import templates
from tests.test_vendor_score import _make_offers, _make_vendor_card

HOVER = "htmx/partials/shared/_score_hover.html"


def _hover_module():
    """Import the macro module so its exported macros can be called directly."""
    return templates.env.get_template(HOVER).module


# ── Breakdown helpers reconcile to the score they explain ──────────────


def test_sighting_breakdown_sums_to_score():
    """score_sighting_v2_breakdown contributions sum to the sighting's total score."""
    total, components = score_sighting_v2(
        vendor_score=80.0,
        is_authorized=False,
        unit_price=1.50,
        median_price=2.00,
        qty_available=1000,
        target_qty=500,
        age_hours=12.0,
        has_price=True,
        has_qty=True,
        has_lead_time=True,
        has_condition=False,
    )
    breakdown = score_sighting_v2_breakdown(components)
    # Five weighted drivers, each a (label, contribution) pair.
    assert [label for label, _ in breakdown] == [
        "Vendor trust",
        "Price competitiveness",
        "Quantity coverage",
        "Freshness",
        "Data completeness",
    ]
    assert abs(sum(c for _, c in breakdown) - total) <= 0.3


def test_sighting_breakdown_skips_missing_factors():
    """A partial components dict yields only the factors present (no crash)."""
    assert score_sighting_v2_breakdown({"trust": 90.0}) == [("Vendor trust", 27.0)]
    assert score_sighting_v2_breakdown({}) == []
    assert score_sighting_v2_breakdown(None) == []


def test_vendor_score_breakdown_reconciles_with_reviews():
    """With reviews, the advancement + review contributions sum to vendor_score."""
    args = dict(offer_count=10, stage_points_sum=40.0, avg_rating=4.0)
    result = compute_vendor_score(**args)
    breakdown = compute_vendor_score_breakdown(**args)
    assert [label for label, _ in breakdown] == ["Order advancement", "Buyer reviews"]
    assert abs(sum(c for _, c in breakdown) - result["vendor_score"]) <= 0.5


def test_vendor_score_breakdown_reconciles_no_reviews_with_dampener():
    """No reviews + cancellation dampener: single contribution reconciles to score."""
    args = dict(
        offer_count=20,
        stage_points_sum=120.0,
        avg_rating=None,
        cancel_count=4,
        slow_cancel_count=2,
        total_pos=10,
    )
    result = compute_vendor_score(**args)
    breakdown = compute_vendor_score_breakdown(**args)
    assert [label for label, _ in breakdown] == ["Order advancement"]
    assert abs(sum(c for _, c in breakdown) - result["vendor_score"]) <= 0.5


def test_vendor_score_breakdown_empty_below_cold_start():
    """Below the offer floor there is no score, so no breakdown."""
    assert compute_vendor_score_breakdown(MIN_OFFERS_FOR_SCORE - 1, 10.0, 4.0) == []


def test_fit_breakdown_sums_to_fit_score():
    """calculate_fit_breakdown contributions sum to calculate_fit_score's number, and
    the six-factor reasoning string format is preserved by the shared refactor."""
    data = {
        "name": "Acme Aerospace",
        "industry": "Aerospace & Defense",
        "naics_code": "334511",
        "employee_count_range": "500-1000",
        "region": "US",
    }
    fit, reasoning = calculate_fit_score(data)
    breakdown = calculate_fit_breakdown(data)
    assert [label for label, _ in breakdown] == [
        "Industry",
        "Company size",
        "Procurement staff",
        "NAICS",
        "Geography",
        "Broker usage",
    ]
    assert sum(c for _, c in breakdown) == fit
    # reasoning is still the "; "-joined six-factor prose (format unchanged by refactor).
    parts = reasoning.split("; ")
    assert len(parts) == 6
    assert parts[0].startswith("Industry:")
    assert parts[1].startswith("Size:")


def test_readiness_breakdown_sums_to_readiness_score():
    """calculate_readiness_breakdown reuses the score's own structured breakdown."""
    signals = {
        "intent": {"strength": "strong"},
        "events": [{"type": "funding round"}],
        "hiring": {"type": "procurement"},
        "new_procurement_hire": True,
        "contacts_verified_count": 3,
    }
    score, _ = calculate_readiness_score({}, signals)
    breakdown = calculate_readiness_breakdown(signals)
    assert [label for label, _ in breakdown] == [
        "Buying intent",
        "Company events",
        "Hiring",
        "New procurement hire",
        "Contact quality",
    ]
    assert sum(c for _, c in breakdown) == score


def test_prospect_breakdown_wrappers_match_stored_score():
    """The prospect-object wrappers reconstruct the exact displayed fit/readiness
    score."""
    prospect = ProspectAccount(
        name="Globex",
        domain="globex.example",
        discovery_source="test",
        industry="Electronics Manufacturing",
        naics_code="334412",
        employee_count_range="200-499",
        region="US",
        readiness_signals={"intent": {"strength": "moderate"}, "contacts_verified_count": 1},
    )
    expected_fit, _ = calculate_fit_score(
        {
            "name": prospect.name,
            "industry": prospect.industry,
            "naics_code": prospect.naics_code,
            "employee_count_range": prospect.employee_count_range,
            "region": prospect.region,
        }
    )
    expected_ready, _ = calculate_readiness_score({}, prospect.readiness_signals)
    assert sum(c for _, c in fit_breakdown_for_prospect(prospect)) == expected_fit
    assert sum(c for _, c in readiness_breakdown_for_prospect(prospect)) == expected_ready


# ── Price-series helper ────────────────────────────────────────────────


def test_price_series_for_card_ordered_and_currency_scoped(db_session):
    """Returns the recorded prices oldest→newest, scoped to the latest currency."""
    card = MaterialCard(normalized_mpn="HOVER-PN-1", display_mpn="HOVER-PN-1")
    db_session.add(card)
    db_session.flush()

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Insert out of order; a EUR row must be excluded (scoped to newest currency = USD).
    for days, price, currency in [(2, 3.00, "USD"), (0, 1.00, "USD"), (1, 99.0, "EUR"), (3, 4.00, "USD")]:
        db_session.add(
            MaterialPriceSnapshot(
                material_card_id=card.id,
                vendor_name="V",
                price=price,
                currency=currency,
                source="api_sighting",
                recorded_at=base + timedelta(days=days),
            )
        )
    db_session.flush()

    series = price_series_for_card(db_session, card.id)
    assert [float(p) for p in series] == [1.00, 3.00, 4.00]  # USD only, chronological


def test_price_series_empty_for_unknown_card(db_session):
    assert price_series_for_card(db_session, 999999) == []


# ── Shared hover macro — three variants render ─────────────────────────


def test_hover_definition_variant_renders():
    html = _hover_module().score_def("Score", "How useful this lead is.")
    assert "Score" in html
    assert 'title="How useful this lead is."' in html
    assert "decoration-dotted" in html
    assert "cursor-help" in html


def test_hover_breakdown_variant_renders():
    html = _hover_module().score_breakdown("80%", [("Industry", 30), ("Company size", 20)], title="Fit factors")
    assert "Fit factors" in html
    assert "Industry" in html
    assert "+30" in html
    assert "80%" in html
    assert 'role="tooltip"' in html  # reuses the Alpine popover convention


def test_hover_breakdown_variant_empty_factors():
    html = _hover_module().score_breakdown("—", [], title="Score factors")
    assert "No breakdown available" in html


def test_hover_price_sparkline_variant_renders():
    html = _hover_module().price_history("$1.50", [1.0, 1.5, 2.0], currency="USD", title="Price history")
    assert "Price history" in html
    assert "<polyline" in html  # the sparkline
    assert "min $1.00" in html
    assert "last $2.00" in html
    assert "max $2.00" in html
    assert "USD" in html
    assert "3 observations" in html


def test_hover_price_sparkline_empty_series():
    html = _hover_module().price_history("$—", [], currency="USD")
    assert "No price history yet" in html


def test_sparkline_svg_variant_renders():
    html = _hover_module().sparkline_svg([1.0, 2.0, 3.0])
    assert "<polyline" in html
    assert "points=" in html


# ── A representative column renders the hover wired to real data (no AI) ──


def test_material_price_history_column_renders_with_real_data():
    """The material price-history tab renders the real-series sparkline + Price header
    definition from actual MaterialPriceSnapshot rows — deterministic, no AI."""
    snaps = [
        MaterialPriceSnapshot(
            material_card_id=1,
            vendor_name="Mouser",
            price=2.00 + i,
            currency="USD",
            source="api_sighting",
            recorded_at=datetime(2026, 2, 1 + i, tzinfo=timezone.utc),
        )
        for i in range(3)
    ]
    html = templates.get_template("htmx/partials/materials/tabs/price_history.html").render(
        snapshots=snaps, card=MaterialCard(normalized_mpn="X", display_mpn="X")
    )
    assert "<polyline" in html  # real-series sparkline overview
    assert "title=" in html  # Price header definition hover
    assert "Mouser" in html


def test_prospecting_card_renders_fit_hover_wired_to_real_data():
    """The prospecting card wires the Fit/Readiness value hover to a real
    ProspectAccount via the registered Jinja globals — no AI, deterministic
    breakdown."""
    prospect = ProspectAccount(
        id=1,
        name="Initech",
        domain="initech.example",
        discovery_source="test",
        status="suggested",
        industry="Electronics Manufacturing",
        naics_code="334412",
        employee_count_range="200-499",
        region="US",
        fit_score=72,
        readiness_score=40,
        readiness_signals={"intent": {"strength": "moderate"}},
        enrichment_data={},
    )
    html = templates.get_template("htmx/partials/prospecting/_card.html").render(
        prospect=prospect, snapshots={}, contact_stats_map={}, status="", can_assign=False
    )
    assert "Fit factors" in html
    assert "Readiness factors" in html
    assert 'role="tooltip"' in html
    # Header-definition hover present on the Fit label.
    assert "ICP fit" in html


# ── Vendor-detail order-advancement score hover (breakdown) ─────────────


def test_vendor_score_breakdown_db_reconciles_with_score(db_session):
    """The DB-access breakdown helper threads the SAME inputs as the score, so its
    contributions reconcile to compute_single_vendor_score's number — no AI."""
    card = _make_vendor_card(db_session, "hover vendor")
    _make_offers(db_session, card.id, "hover vendor", 6)  # above the cold-start floor
    db_session.commit()

    score = compute_single_vendor_score(db_session, card.id)["vendor_score"]
    breakdown = compute_single_vendor_score_breakdown(db_session, card.id)
    assert score is not None
    assert breakdown  # non-empty above the floor
    assert breakdown[0][0] == "Order advancement"
    assert abs(sum(c for _, c in breakdown) - score) <= 0.5


def test_vendor_score_breakdown_db_empty_below_floor_and_unknown(db_session):
    """No score → no breakdown: below the offer floor and for an unknown vendor."""
    cold = _make_vendor_card(db_session, "cold vendor")
    _make_offers(db_session, cold.id, "cold vendor", MIN_OFFERS_FOR_SCORE - 1)
    db_session.commit()
    assert compute_single_vendor_score_breakdown(db_session, cold.id) == []
    assert compute_single_vendor_score_breakdown(db_session, 999999) == []


def test_vendor_detail_renders_score_hover_wired_to_real_data():
    """Vendor detail renders the Score header definition + the value breakdown popover
    wired to a real (label, contribution) list — deterministic, no AI call."""
    vendor = VendorCard(
        id=1,
        normalized_name="acme corp",
        display_name="Acme Corp",
        vendor_score=56.0,
        is_blacklisted=False,
        is_active=True,
        total_pos=10,
    )
    # Real breakdown from the same weights the score uses (reconciles to 56.0).
    breakdown = compute_vendor_score_breakdown(offer_count=10, stage_points_sum=40.0, avg_rating=4.0)
    html = templates.get_template("htmx/partials/vendors/detail.html").render(
        vendor=vendor,
        contacts=[],
        recent_sightings=[],
        safety_band=None,
        mpn_filter=None,
        cadence_state="new",
        next_best_touch="Reach out soon",
        now_utc=datetime(2026, 3, 1, tzinfo=timezone.utc),
        vendor_score_breakdown=breakdown,
    )
    # Value breakdown popover wired to the real drivers.
    assert 'role="tooltip"' in html
    assert "Score factors" in html
    assert "Order advancement" in html
    assert "Buyer reviews" in html
    # Header definition hover on the "Score" label.
    assert 'title="Vendor score (0-100):' in html
    assert "decoration-dotted" in html


# ── Prospecting AI-screen trio — def-only hover (no fabricated breakdown) ──


def test_prospecting_card_ai_match_renders_def_only_hover():
    """The AI Match label carries a header-definition hover only (AI score, no
    deterministic decomposition) — never a fabricated breakdown popover."""
    prospect = ProspectAccount(
        id=1,
        name="Initech",
        domain="initech.example",
        discovery_source="test",
        status="suggested",
        enrichment_data={"ai_screen": {"verdict": "pass", "trio_match_score": 82, "rationale": "Aerospace OEM."}},
    )
    html = templates.get_template("htmx/partials/prospecting/_card.html").render(
        prospect=prospect, snapshots={}, contact_stats_map={}, status="", can_assign=False
    )
    assert "AI Match" in html
    assert "82%" in html
    # Definition tooltip (score_def), not a breakdown popover.
    assert 'title="AI procurement-fit screen (0-100):' in html
    assert "decoration-dotted" in html


def test_prospecting_detail_ai_trio_renders_def_only_hover():
    """The AI Screening trio (Match / Opportunity / Excess) each get a header-definition
    hover — AI scores with no weighted breakdown, so def-only (no fabrication)."""
    prospect = ProspectAccount(
        id=1,
        name="Initech",
        domain="initech.example",
        discovery_source="test",
        status="claimed",
        fit_score=72,
        readiness_score=40,
        readiness_signals={"intent": {"strength": "moderate"}},
        enrichment_data={},
    )
    enrichment = {
        "ai_screen": {
            "verdict": "pass",
            "trio_match_score": 82,
            "opportunity_score": 65,
            "excess_likelihood": 30,
            "rationale": "Aerospace OEM with verified procurement contact.",
            "evidence": ["industry=Aerospace & Defense"],
            "confidence": 80,
            "model": "claude",
        }
    }
    html = templates.get_template("htmx/partials/prospecting/detail.html").render(
        prospect=prospect,
        enrichment=enrichment,
        snapshot={},
        signal_tags=[],
        contacts=[],
        contact_stats={},
        similar_customers=[],
        warm_intro={},
        enrich_state=None,
        can_assign=False,
    )
    assert "AI Screening" in html
    # Each trio label carries its definitional tooltip (def-only).
    assert 'title="AI procurement-fit screen (0-100):' in html
    assert 'title="AI opportunity size (0-100):' in html
    assert 'title="AI excess-inventory likelihood (0-100):' in html
    assert "decoration-dotted" in html
    # Raw AI numbers still render (opportunity=65, excess=30 are distinctive).
    assert "65" in html
    assert "30" in html

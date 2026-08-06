"""Tests for the proactive matching engine (2026-08-06 requirement-seeded rework).

Covers: part identity (verbatim, variants stay separate), the live-supply
rollup (SUM qty / MIN positive price / 7-day window), the ask-based scoring
composite, requirement-window seeding across every requisition status,
back-order routing, suppression (dedup / do-not-offer / throttle / CPH margin
gate), the batch scan + watermark, and the retained match actions.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.constants import ProactiveMatchSource, ProactiveMatchStatus
from app.models import (
    Company,
    CustomerSite,
    Offer,
    ProactiveMatch,
    Requirement,
    Requisition,
    User,
)
from app.models.intelligence import ProactiveDoNotOffer, ProactiveThrottle
from app.models.purchase_history import CustomerPartHistory
from app.services.proactive_matching import (
    _score_ask_recency,
    _score_margin,
    _score_qty_fit,
    _score_quote_spread,
    _score_recent_win,
    _score_repeat_demand,
    compute_match_score,
    compute_offer_rollup,
    dismiss_match,
    expire_old_matches,
    find_matches_for_offer,
    mark_match_sent,
    part_key,
    run_proactive_scan,
    trigger_rematch_on_offer_approval,
)
from tests.conftest import engine  # noqa: F401

MPN = "STM32F407"


def _setup_scenario(db, *, req_status="open", account_owner=True, company_on_req=False):
    """Company + site + owner + one requisition/requirement asking for MPN.

    The requisition links the customer via its site by default (the common
    shape in real data); ``company_on_req=True`` also sets requisition.company_id.
    """
    owner = User(
        email="sales@trioscs.com",
        name="Sales Rep",
        role="sales",
        azure_id="sales-001",
        created_at=datetime.now(UTC),
    )
    db.add(owner)
    db.flush()

    company = Company(
        name="Sensata Technologies",
        is_active=True,
        account_owner_id=owner.id if account_owner else None,
    )
    db.add(company)
    db.flush()

    site = CustomerSite(company_id=company.id, site_name="Sensata HQ", is_active=True)
    db.add(site)
    db.flush()

    req = Requisition(
        name="Test Req",
        customer_site_id=site.id,
        company_id=company.id if company_on_req else None,
        status=req_status,
        created_by=owner.id,
    )
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn=MPN,
        normalized_mpn="stm32f407",
        target_qty=1000,
        created_at=datetime.now(UTC) - timedelta(days=20),
    )
    db.add(requirement)
    db.commit()

    return {
        "owner": owner,
        "company": company,
        "site": site,
        "requisition": req,
        "requirement": requirement,
    }


def _make_offer(db, **overrides):
    """Add a live in-window offer for MPN and return it."""
    fields = {
        "vendor_name": "Arrow",
        "mpn": MPN,
        "qty_available": 500,
        "unit_price": Decimal("8.00"),
        "status": "active",
    }
    fields.update(overrides)
    offer = Offer(**fields)
    db.add(offer)
    db.commit()
    return offer


# ── Part identity ────────────────────────────────────────────────────────


def test_part_key_uppercases_and_trims():
    assert part_key("  ltsr15-np ") == "LTSR15-NP"


def test_part_key_preserves_interior_space():
    """Spelling variants stay separate materials — never merged silently."""
    assert part_key("LTSR 15-NP") == "LTSR 15-NP"
    assert part_key("LTSR 15-NP") != part_key("LTSR15-NP")


def test_part_key_empty_is_none():
    assert part_key(None) is None
    assert part_key("   ") is None


# ── Supply rollup ────────────────────────────────────────────────────────


def test_rollup_sums_qty_and_takes_min_positive_price(db_session):
    _make_offer(db_session, vendor_name="A", qty_available=1000, unit_price=Decimal("7.50"))
    _make_offer(db_session, vendor_name="B", qty_available=850, unit_price=Decimal("6.44"))
    _make_offer(db_session, vendor_name="C", qty_available=3000, unit_price=Decimal("9.00"))
    r = compute_offer_rollup(db_session, part=MPN)
    assert r["offer_count"] == 3
    assert r["available_qty"] == 4850
    assert r["low_cost"] == 6.44


def test_rollup_zero_price_counts_qty_but_not_low_cost(db_session):
    """$0.00 = price not provided — included in availability, excluded from low cost."""
    _make_offer(db_session, vendor_name="A", qty_available=177630, unit_price=Decimal("0"))
    _make_offer(db_session, vendor_name="B", qty_available=100, unit_price=Decimal("0.05"))
    r = compute_offer_rollup(db_session, part=MPN)
    assert r["available_qty"] == 177730
    assert r["low_cost"] == 0.05


def test_rollup_all_zero_prices_has_no_low_cost(db_session):
    _make_offer(db_session, qty_available=10, unit_price=Decimal("0"))
    r = compute_offer_rollup(db_session, part=MPN)
    assert r["low_cost"] is None


def test_rollup_excludes_offers_outside_window(db_session):
    _make_offer(db_session, qty_available=100, unit_price=Decimal("5.00"))
    _make_offer(
        db_session,
        vendor_name="Old",
        qty_available=9999,
        unit_price=Decimal("1.00"),
        created_at=datetime.now(UTC) - timedelta(days=10),
    )
    r = compute_offer_rollup(db_session, part=MPN)
    assert r["offer_count"] == 1
    assert r["available_qty"] == 100
    assert r["low_cost"] == 5.0


def test_rollup_excludes_non_live_statuses(db_session):
    _make_offer(db_session, qty_available=100)
    _make_offer(db_session, vendor_name="P", qty_available=50, status="pending_review")
    _make_offer(db_session, vendor_name="S", qty_available=50, status="sold")
    r = compute_offer_rollup(db_session, part=MPN)
    assert r["offer_count"] == 1


def test_rollup_variant_spelling_stays_separate(db_session):
    _make_offer(db_session, mpn="LTSR15-NP", qty_available=4850, unit_price=Decimal("6.44"))
    _make_offer(db_session, mpn="LTSR 15-NP", qty_available=1000, unit_price=Decimal("10.00"))
    exact = compute_offer_rollup(db_session, part="LTSR15-NP")
    variant = compute_offer_rollup(db_session, part="LTSR 15-NP")
    assert exact["available_qty"] == 4850
    assert variant["available_qty"] == 1000


# ── Scoring units ────────────────────────────────────────────────────────


def test_ask_recency_tiers():
    now = datetime.now(UTC)
    assert _score_ask_recency(now - timedelta(days=10)) == 100
    assert _score_ask_recency(now - timedelta(days=60)) == 80
    assert _score_ask_recency(now - timedelta(days=200)) == 60


def test_ask_recency_old_ask_floors_not_zero():
    """An early-2025 archived ask with fresh supply is interesting, not stale."""
    assert _score_ask_recency(datetime.now(UTC) - timedelta(days=600)) == 40
    assert _score_ask_recency(None) == 40


def test_repeat_demand_tiers():
    assert _score_repeat_demand(1) == 40
    assert _score_repeat_demand(2) == 60
    assert _score_repeat_demand(4) == 80
    assert _score_repeat_demand(7) == 100


def test_quote_spread_tiers():
    assert _score_quote_spread(None) == 50
    assert _score_quote_spread(35.0) == 100
    assert _score_quote_spread(15.0) == 60
    assert _score_quote_spread(5.0) == 40
    assert _score_quote_spread(-3.0) == 10


def test_recent_win_tiers():
    assert _score_recent_win("same_customer") == 100
    assert _score_recent_win("other_customer") == 80
    assert _score_recent_win(None) == 40


def test_qty_fit_tiers():
    assert _score_qty_fit(1000, 1000) == 100
    assert _score_qty_fit(600, 1000) == 70
    assert _score_qty_fit(100, 1000) == 40
    assert _score_qty_fit(500, None) == 70  # ask size unknown


def test_composite_score_bounds():
    high = compute_match_score(
        last_asked_at=datetime.now(UTC) - timedelta(days=5),
        requirement_count=7,
        available_qty=10_000,
        last_asked_qty=500,
        quote_spread_pct=40.0,
        recent_win="same_customer",
    )
    assert high == 100
    low = compute_match_score(
        last_asked_at=datetime.now(UTC) - timedelta(days=700),
        requirement_count=1,
        available_qty=10,
        last_asked_qty=1000,
        quote_spread_pct=-5.0,
        recent_win=None,
    )
    assert 0 <= low < 40


def test_margin_context_retained():
    score, pct = _score_margin(10.0, 8.0)
    assert pct == 20.0
    assert score == 80
    score, pct = _score_margin(None, 8.0)
    assert pct is None


# ── Requirement-seeded matching ──────────────────────────────────────────


def test_find_matches_from_requirement_history(db_session):
    data = _setup_scenario(db_session)
    offer = _make_offer(db_session, unit_price=Decimal("6.44"), qty_available=850)
    _make_offer(db_session, vendor_name="B", unit_price=Decimal("7.10"), qty_available=4000)

    matches = find_matches_for_offer(offer.id, db_session)
    db_session.commit()

    assert len(matches) == 1
    m = matches[0]
    assert m.company_id == data["company"].id
    assert m.salesperson_id == data["owner"].id
    assert m.mpn == MPN
    assert m.match_source == ProactiveMatchSource.REQUIREMENT
    assert m.requirement_count == 1
    assert m.last_asked_qty == 1000
    assert m.requirement_id == data["requirement"].id
    assert float(m.our_cost) == 6.44  # low cost across live offers, not this offer's price


def test_find_matches_counts_repeat_asks(db_session):
    data = _setup_scenario(db_session)
    req2 = Requisition(
        name="Second ask",
        customer_site_id=data["site"].id,
        status="lost",
        created_by=data["owner"].id,
    )
    db_session.add(req2)
    db_session.flush()
    newest = Requirement(
        requisition_id=req2.id,
        primary_mpn=MPN,
        target_qty=3000,
        created_at=datetime.now(UTC) - timedelta(days=3),
    )
    db_session.add(newest)
    db_session.commit()

    offer = _make_offer(db_session)
    matches = find_matches_for_offer(offer.id, db_session)
    db_session.commit()

    assert len(matches) == 1  # ONE line per part per customer — never one per ask
    m = matches[0]
    assert m.requirement_count == 2
    assert m.last_asked_qty == 3000  # newest ask wins
    assert m.requirement_id == newest.id


@pytest.mark.parametrize("status", ["won", "lost", "cancelled", "quoted"])
def test_closed_requisitions_still_seed(db_session, status):
    """A won/lost ask is demand history — exactly what this feature exists to catch."""
    _setup_scenario(db_session, req_status=status)
    offer = _make_offer(db_session)
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 1


def test_requirement_outside_window_does_not_seed(db_session):
    data = _setup_scenario(db_session)
    data["requirement"].created_at = datetime.now(UTC) - timedelta(days=800)
    db_session.commit()
    offer = _make_offer(db_session)
    assert find_matches_for_offer(offer.id, db_session) == []


def test_scratch_requisitions_do_not_seed(db_session):
    data = _setup_scenario(db_session)
    data["requisition"].is_scratch = True
    db_session.commit()
    offer = _make_offer(db_session)
    assert find_matches_for_offer(offer.id, db_session) == []


def test_company_on_requisition_directly(db_session):
    data = _setup_scenario(db_session, company_on_req=True)
    offer = _make_offer(db_session)
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 1
    assert matches[0].company_id == data["company"].id


def test_owner_fallback_when_company_has_no_account_owner(db_session):
    """Ownerless companies route to the newest ask's requisition owner, not skipped."""
    data = _setup_scenario(db_session, account_owner=False)
    offer = _make_offer(db_session)
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 1
    assert matches[0].salesperson_id == data["owner"].id


def test_backorder_requirement_routes_to_requisition_owner(db_session):
    owner = User(email="t@trioscs.com", name="Trader", role="trader", azure_id="t-1")
    db_session.add(owner)
    db_session.flush()
    req = Requisition(name="Back order", status="open", created_by=owner.id)
    db_session.add(req)
    db_session.flush()
    db_session.add(Requirement(requisition_id=req.id, primary_mpn=MPN, target_qty=50))
    db_session.commit()

    offer = _make_offer(db_session)
    matches = find_matches_for_offer(offer.id, db_session)
    db_session.commit()
    assert len(matches) == 1
    assert matches[0].company_id is None
    assert matches[0].salesperson_id == owner.id

    # Dedup holds for back-order lines too
    offer2 = _make_offer(db_session, vendor_name="B")
    assert find_matches_for_offer(offer2.id, db_session) == []


def test_dedup_one_active_match_per_part_customer(db_session):
    _setup_scenario(db_session)
    offer = _make_offer(db_session)
    assert len(find_matches_for_offer(offer.id, db_session)) == 1
    db_session.commit()
    offer2 = _make_offer(db_session, vendor_name="Second Vendor")
    assert find_matches_for_offer(offer2.id, db_session) == []


def test_do_not_offer_suppression(db_session):
    data = _setup_scenario(db_session)
    db_session.add(ProactiveDoNotOffer(mpn=MPN, company_id=data["company"].id))
    db_session.commit()
    offer = _make_offer(db_session)
    assert find_matches_for_offer(offer.id, db_session) == []


def test_throttle_suppression(db_session):
    data = _setup_scenario(db_session)
    db_session.add(
        ProactiveThrottle(
            mpn=MPN,
            customer_site_id=data["site"].id,
            last_offered_at=datetime.now(UTC) - timedelta(days=5),
        )
    )
    db_session.commit()
    offer = _make_offer(db_session)
    assert find_matches_for_offer(offer.id, db_session) == []


def test_margin_gate_applies_only_with_purchase_history(db_session):
    """CPH prices the customer → thin margin suppresses.

    A target price never does.
    """
    data = _setup_scenario(db_session)
    card_id = None
    offer = _make_offer(db_session, unit_price=Decimal("9.90"))
    # No CPH: match survives even though target price (implicit) is irrelevant
    assert len(find_matches_for_offer(offer.id, db_session)) == 1
    db_session.query(ProactiveMatch).delete()
    db_session.commit()

    # CPH says customer pays ~$10 and our cost is $9.90 → 1% margin < 10% gate
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="stm32f407", display_mpn=MPN)
    db_session.add(card)
    db_session.flush()
    card_id = card.id
    data["requirement"].material_card_id = card_id
    db_session.add(
        CustomerPartHistory(
            company_id=data["company"].id,
            material_card_id=card_id,
            mpn=MPN,
            source="buy_plan",
            purchase_count=3,
            avg_unit_price=Decimal("10.00"),
            last_unit_price=Decimal("10.00"),
            last_purchased_at=datetime.now(UTC) - timedelta(days=90),
        )
    )
    db_session.commit()
    assert find_matches_for_offer(offer.id, db_session) == []


def test_cph_enriches_match_as_context(db_session):
    data = _setup_scenario(db_session)
    from app.models import MaterialCard

    card = MaterialCard(normalized_mpn="stm32f407", display_mpn=MPN)
    db_session.add(card)
    db_session.flush()
    data["requirement"].material_card_id = card.id
    db_session.add(
        CustomerPartHistory(
            company_id=data["company"].id,
            material_card_id=card.id,
            mpn=MPN,
            source="buy_plan",
            purchase_count=4,
            avg_unit_price=Decimal("20.00"),
            last_unit_price=Decimal("19.00"),
            last_purchased_at=datetime.now(UTC) - timedelta(days=45),
        )
    )
    db_session.commit()
    offer = _make_offer(db_session, unit_price=Decimal("8.00"))
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 1
    m = matches[0]
    assert m.customer_purchase_count == 4
    assert float(m.customer_last_price) == 19.0
    assert m.margin_pct is not None


def test_variant_spelling_does_not_cross_match(db_session):
    """An offer under 'LTSR 15-NP' must not match a 'LTSR15-NP' requirement."""
    data = _setup_scenario(db_session)
    data["requirement"].primary_mpn = "LTSR15-NP"
    db_session.commit()
    offer = _make_offer(db_session, mpn="LTSR 15-NP")
    assert find_matches_for_offer(offer.id, db_session) == []


def test_offer_without_material_card_still_matches(db_session):
    """Card resolution is enrichment, not a prerequisite (seeded SF offers may lack
    cards)."""
    _setup_scenario(db_session)
    offer = _make_offer(db_session, material_card_id=None)
    assert len(find_matches_for_offer(offer.id, db_session)) == 1


# ── Hotlist seeding ──────────────────────────────────────────────────────


def _setup_hotlist(db, *, account_owner=True):
    data = _setup_scenario(db, req_status="hotlist", account_owner=account_owner)
    return data


def test_hotlist_seeds_match(db_session):
    data = _setup_hotlist(db_session)
    offer = _make_offer(db_session)
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 1
    m = matches[0]
    assert m.match_source == ProactiveMatchSource.HOTLIST
    assert m.match_score == 60
    assert m.salesperson_id == data["owner"].id


def test_hotlist_owner_fallback(db_session):
    data = _setup_hotlist(db_session, account_owner=False)
    offer = _make_offer(db_session)
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 1
    assert matches[0].salesperson_id == data["owner"].id


def test_hotlist_not_double_matched_with_requirement_pass(db_session):
    """A customer with both a windowed ask and a hotlist gets ONE line."""
    data = _setup_scenario(db_session)
    hot = Requisition(
        name="Hotlist",
        customer_site_id=data["site"].id,
        company_id=data["company"].id,
        status="hotlist",
        created_by=data["owner"].id,
    )
    db_session.add(hot)
    db_session.flush()
    db_session.add(Requirement(requisition_id=hot.id, primary_mpn=MPN))
    db_session.commit()
    offer = _make_offer(db_session)
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 1
    assert matches[0].match_source == ProactiveMatchSource.REQUIREMENT


# ── Batch scan ───────────────────────────────────────────────────────────


def test_run_proactive_scan(db_session):
    _setup_scenario(db_session)
    _make_offer(db_session)
    with patch("app.services.proactive_matching._get_watermark") as mock_wm:
        mock_wm.return_value = datetime.now(UTC) - timedelta(hours=1)
        result = run_proactive_scan(db_session)
    assert result["scanned_offers"] == 1
    assert result["matches_created"] == 1


def test_run_proactive_scan_dedups_parts(db_session):
    _setup_scenario(db_session)
    _make_offer(db_session, vendor_name="A")
    _make_offer(db_session, vendor_name="B")
    with patch("app.services.proactive_matching._get_watermark") as mock_wm:
        mock_wm.return_value = datetime.now(UTC) - timedelta(hours=1)
        result = run_proactive_scan(db_session)
    assert result["scanned_offers"] == 2
    assert result["matches_created"] == 1  # one part → one pass → one line


def test_scan_ignores_offers_before_watermark(db_session):
    _setup_scenario(db_session)
    _make_offer(db_session, created_at=datetime.now(UTC) - timedelta(hours=6))
    with patch("app.services.proactive_matching._get_watermark") as mock_wm:
        mock_wm.return_value = datetime.now(UTC) - timedelta(hours=1)
        result = run_proactive_scan(db_session)
    assert result["scanned_offers"] == 0


def test_trigger_rematch_on_offer_approval(db_session):
    _setup_scenario(db_session)
    offer = _make_offer(db_session, status="approved")
    created = trigger_rematch_on_offer_approval(db_session, offer)
    assert created == 1


# ── Match actions (retained behavior) ────────────────────────────────────


def _one_match(db):
    _setup_scenario(db)
    offer = _make_offer(db)
    matches = find_matches_for_offer(offer.id, db)
    db.commit()
    return matches[0]


def test_dismiss_match(db_session):
    m = _one_match(db_session)
    dismiss_match(m.id, m.salesperson_id, "not interested", db_session)
    db_session.refresh(m)
    assert m.status == ProactiveMatchStatus.DISMISSED
    assert m.dismiss_reason == "not interested"


def test_dismiss_match_wrong_user(db_session):
    m = _one_match(db_session)
    with pytest.raises(ValueError, match="Not your match"):
        dismiss_match(m.id, m.salesperson_id + 999, "nope", db_session)


def test_mark_match_sent(db_session):
    m = _one_match(db_session)
    mark_match_sent(m.id, m.salesperson_id, db_session)
    db_session.refresh(m)
    assert m.status == ProactiveMatchStatus.SENT


def test_expire_old_matches(db_session):
    m = _one_match(db_session)
    m.created_at = datetime.now(UTC) - timedelta(days=45)
    db_session.commit()
    assert expire_old_matches(db_session) == 1
    db_session.refresh(m)
    assert m.status == ProactiveMatchStatus.EXPIRED

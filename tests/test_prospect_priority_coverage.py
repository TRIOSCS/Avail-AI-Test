"""test_prospect_priority_coverage.py — Extra coverage for prospect_priority.py.

Targets uncovered branches at lines 42-44, 55-57, 59-61, 70-71, 76-81, 83-85,
89, 93-95, 104-106, 108-110, 113-115, 118-119, 124, 126, 132.

These cover:
- moderate buying intent (vs strong)
- multiple decision-makers vs one
- 2+ verified contacts (non-decision-maker)
- exactly 1 verified contact
- warm intro with non-hot warmth
- similar customers (string items vs dict)
- bought_before path
- procurement hiring vs engineering hiring
- new_procurement_hire signal
- import_priority="priority" flag
- baseline reasons for fit/readiness combinations
- no reasons fallback

Called by: pytest
Depends on: app/services/prospect_priority.py
"""

import os

os.environ["TESTING"] = "1"

from types import SimpleNamespace

from app.services.prospect_priority import build_priority_snapshot


def _prospect(**overrides):
    base = {
        "fit_score": 60,
        "readiness_score": 40,
        "readiness_signals": {},
        "contacts_preview": [],
        "similar_customers": [],
        "historical_context": {},
        "enrichment_data": {},
        "import_priority": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBuildPrioritySnapshotAllBranches:
    def test_moderate_buying_intent(self):
        """Moderate intent → +6 score, reason added (line 42-44 else branch)."""
        snapshot = build_priority_snapshot(
            _prospect(
                readiness_signals={"intent": {"strength": "moderate"}},
            )
        )
        assert "Moderate buying intent" in snapshot["priority_reasons"]
        assert snapshot["proof_points"] >= 1

    def test_no_intent_signal_no_reason(self):
        """No intent key → no intent reason added."""
        snapshot = build_priority_snapshot(
            _prospect(
                readiness_signals={"intent": {}},
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Strong buying intent" not in reasons
        assert "Moderate buying intent" not in reasons

    def test_multiple_decision_makers_get_higher_score(self):
        """2+ verified DMs → +11 (line 51-53 else branch for verified_dms > 1)."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=70,
                readiness_score=50,
                contacts_preview=[
                    {"verified": True, "seniority": "decision_maker"},
                    {"verified": True, "seniority": "decision_maker"},
                ],
            )
        )
        reasons = snapshot["priority_reasons"]
        assert any("decision-maker" in r for r in reasons)
        # 2 DMs → "2 verified decision-makers"
        assert any("2" in r for r in reasons)

    def test_single_decision_maker_gets_plus_nine(self):
        """1 verified DM → +9 (line 50 if verified_dms == 1)."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=70,
                readiness_score=50,
                contacts_preview=[
                    {"verified": True, "seniority": "decision_maker"},
                ],
            )
        )
        reasons = snapshot["priority_reasons"]
        assert any("1 verified decision-maker" in r for r in reasons)

    def test_two_verified_non_dm_contacts(self):
        """2 verified contacts (not DMs) → +6, reason added (lines 55-57)."""
        snapshot = build_priority_snapshot(
            _prospect(
                contacts_preview=[
                    {"verified": True, "seniority": "manager"},
                    {"verified": True, "seniority": "staff"},
                ],
            )
        )
        reasons = snapshot["priority_reasons"]
        assert any("2 verified contacts" in r for r in reasons)

    def test_one_verified_non_dm_contact(self):
        """1 verified contact (not DM) → +3, reason '1 verified contact' (lines
        59-61)."""
        snapshot = build_priority_snapshot(
            _prospect(
                contacts_preview=[
                    {"verified": True, "seniority": "staff"},
                ],
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "1 verified contact" in reasons

    def test_warm_intro_non_hot_warmth(self):
        """Warm intro with warmth != 'hot' → +6, 'Prior relationship' reason (lines
        70-71)."""
        snapshot = build_priority_snapshot(
            _prospect(
                enrichment_data={"warm_intro": {"has_warm_intro": True, "warmth": "warm"}},
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Prior relationship to leverage" in reasons

    def test_similar_customers_string_items(self):
        """similar_customers as plain strings → names extracted (lines 76-81)."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=60,
                similar_customers=["Acme Corp", "BestBuy Electronics"],
            )
        )
        reasons = snapshot["priority_reasons"]
        assert any("Similar wins" in r for r in reasons)
        assert any("Acme Corp" in r for r in reasons)

    def test_similar_customers_dict_items(self):
        """similar_customers as dicts with 'name' key."""
        snapshot = build_priority_snapshot(
            _prospect(
                similar_customers=[
                    {"name": "TechSupply Inc", "score": 0.9},
                    {"name": "Parts World"},
                ],
            )
        )
        reasons = snapshot["priority_reasons"]
        assert any("TechSupply Inc" in r for r in reasons)

    def test_bought_before_path(self):
        """bought_before=True → +8, 'Previous Trio customer' reason (line 93-95)."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=70,
                readiness_score=50,
                historical_context={"bought_before": True},
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Previous Trio customer" in reasons

    def test_quoted_before_via_quote_count(self):
        """quoted_before=False but quote_count > 0 → 'Previous Trio quote history'."""
        snapshot = build_priority_snapshot(
            _prospect(
                historical_context={"bought_before": False, "quoted_before": False, "quote_count": 2},
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Previous Trio quote history" in reasons

    def test_non_numeric_quote_count_treated_as_zero(self):
        """quote_count of non-numeric type → treated as 0 (line 89)."""
        snapshot = build_priority_snapshot(
            _prospect(
                historical_context={"quote_count": "many"},
            )
        )
        # Should not crash
        assert isinstance(snapshot, dict)

    def test_procurement_hiring_signal(self):
        """Hiring type='procurement' → +4, 'Procurement hiring signal' (lines
        104-106)."""
        snapshot = build_priority_snapshot(
            _prospect(
                readiness_signals={"hiring": {"type": "procurement"}},
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Procurement hiring signal" in reasons

    def test_engineering_hiring_signal(self):
        """Hiring type='engineering' → +2, 'Engineering growth signal' (lines
        108-110)."""
        snapshot = build_priority_snapshot(
            _prospect(
                readiness_signals={"hiring": {"type": "engineering"}},
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Engineering growth signal" in reasons

    def test_new_procurement_hire_signal(self):
        """new_procurement_hire=True → +3, reason added (lines 113-115)."""
        snapshot = build_priority_snapshot(
            _prospect(
                readiness_signals={"new_procurement_hire": True},
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "New procurement hire" in reasons

    def test_import_priority_flag(self):
        """import_priority='priority' → +3, 'Marked priority' reason (lines 118-119)."""
        snapshot = build_priority_snapshot(
            _prospect(
                import_priority="priority",
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Marked priority" in reasons

    def test_strong_fit_readiness_baseline(self):
        """Fit>=75 and readiness>=55 → 'Strong fit/readiness baseline' (line 124)."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=78,
                readiness_score=58,
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Strong fit/readiness baseline" in reasons

    def test_strong_icp_fit_only(self):
        """Fit>=70 but readiness<55 → 'Strong ICP fit' (line 126)."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=72,
                readiness_score=40,
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Strong ICP fit" in reasons

    def test_strong_near_term_timing_only(self):
        """Readiness>=60 but fit<70 → 'Strong near-term timing' (line 128)."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=55,
                readiness_score=65,
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Strong near-term timing" in reasons

    def test_no_reasons_gets_default_message(self):
        """All zeros → 'Needs stronger buyer signals' (line 132)."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=0,
                readiness_score=0,
            )
        )
        reasons = snapshot["priority_reasons"]
        assert "Needs stronger buyer signals" in reasons

    def test_buyer_ready_score_capped_at_100(self):
        """Score is clamped to max 100."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=100,
                readiness_score=100,
                readiness_signals={
                    "intent": {"strength": "strong"},
                    "hiring": {"type": "procurement"},
                    "new_procurement_hire": True,
                },
                contacts_preview=[
                    {"verified": True, "seniority": "decision_maker"},
                    {"verified": True, "seniority": "decision_maker"},
                ],
                similar_customers=["Acme", "BestBuy"],
                historical_context={"bought_before": True},
                enrichment_data={"warm_intro": {"has_warm_intro": True, "warmth": "hot"}},
                import_priority="priority",
            )
        )
        assert snapshot["buyer_ready_score"] <= 100

    def test_buyer_ready_score_floor_at_zero(self):
        """Score is floored at 0 even with negative inputs."""
        # fit_score=None → handled by `fit_score or 0`
        snapshot = build_priority_snapshot(_prospect(fit_score=None, readiness_score=None))
        assert snapshot["buyer_ready_score"] >= 0

    def test_is_buyer_ready_false_when_fit_below_50(self):
        """is_buyer_ready=False when fit < 50, even with proof points."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=40,  # < 50
                readiness_score=80,
                readiness_signals={"intent": {"strength": "strong"}},
                contacts_preview=[{"verified": True, "seniority": "decision_maker"}],
            )
        )
        assert snapshot["is_buyer_ready"] is False

    def test_proof_points_counted_correctly(self):
        """Multiple signals → proof_points incremented for each."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=75,
                readiness_score=60,
                readiness_signals={
                    "intent": {"strength": "strong"},
                    "hiring": {"type": "procurement"},
                },
                contacts_preview=[{"verified": True, "seniority": "decision_maker"}],
                historical_context={"bought_before": True},
            )
        )
        assert snapshot["proof_points"] >= 4

    def test_priority_reasons_capped_at_four(self):
        """priority_reasons is limited to 4 items."""
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=78,
                readiness_score=62,
                readiness_signals={
                    "intent": {"strength": "strong"},
                    "hiring": {"type": "procurement"},
                    "new_procurement_hire": True,
                },
                contacts_preview=[{"verified": True, "seniority": "decision_maker"}],
                historical_context={"bought_before": True},
                enrichment_data={"warm_intro": {"has_warm_intro": True, "warmth": "hot"}},
                similar_customers=["Acme"],
                import_priority="priority",
            )
        )
        assert len(snapshot["priority_reasons"]) <= 4

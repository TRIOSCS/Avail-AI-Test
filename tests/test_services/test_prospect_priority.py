"""test_prospect_priority.py — Tests buyer-ready prospect prioritization helpers.

Verifies the explainable lead-priority rules used by suggested prospects.
Called by: pytest
Depends on: app/services/prospect_priority.py
"""

from types import SimpleNamespace

from app.services.prospect_priority import build_priority_snapshot


def _prospect(**overrides):
    base = {
        "fit_score": 70,
        "readiness_score": 50,
        "readiness_signals": {},
        "contacts_preview": [],
        "similar_customers": [],
        "historical_context": {},
        "enrichment_data": {},
        "import_priority": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBuildPrioritySnapshot:
    def test_marks_actionable_prospect_as_buyer_ready(self):
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=78,
                readiness_score=64,
                readiness_signals={"intent": {"strength": "strong"}},
                contacts_preview=[
                    {"name": "Jane Buyer", "verified": True, "seniority": "decision_maker"},
                ],
            )
        )
        assert snapshot["buyer_ready_score"] >= 70
        assert snapshot["is_buyer_ready"] is True
        assert "Strong buying intent" in snapshot["priority_reasons"]

    def test_high_scores_without_proof_do_not_count_as_buyer_ready(self):
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=92,
                readiness_score=72,
                readiness_signals={},
                contacts_preview=[],
            )
        )
        assert snapshot["buyer_ready_score"] >= 70
        assert snapshot["is_buyer_ready"] is False

    def test_warm_intro_and_history_raise_priority(self):
        snapshot = build_priority_snapshot(
            _prospect(
                fit_score=65,
                readiness_score=48,
                historical_context={"quoted_before": True, "quote_count": 4},
                enrichment_data={"warm_intro": {"has_warm_intro": True, "warmth": "hot"}},
            )
        )
        assert snapshot["buyer_ready_score"] >= 70
        assert snapshot["is_buyer_ready"] is True
        assert "Warm intro available" in snapshot["priority_reasons"]

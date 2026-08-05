"""tests/test_buyplan_state.py — BuyPlan status state machine (W3 consolidation).

Covers app/services/buyplan_workflow/buyplan_state.py: the formalized transition
table matches the graph the 9 inline writers implemented, transition() applies
legal edges / raises on illegal ones / no-ops on same-status, and the writer
frozensets in buyplan_approval.py agree with the table (guard-vs-table drift).

Called by: pytest
Depends on: buyplan_state (pure logic — no DB needed; plan is a stub)
"""

import pytest

from app.constants import BuyPlanStatus
from app.services.buyplan_workflow.buyplan_approval import (
    HALTABLE_STATUSES,
    RESUBMITTABLE_STATUSES,
)
from app.services.buyplan_workflow.buyplan_state import ALLOWED_TRANSITIONS, transition


class _Plan:
    """Minimal stand-in: transition() only reads/writes .status (+ .id for the log)."""

    def __init__(self, status: str):
        self.id = 1
        self.status = status


# Every edge a writer actually takes (writer → edge), straight from the W3 recon map.
_WRITER_EDGES = [
    ("draft", "pending"),  # submit_buy_plan / resubmit_buy_plan
    ("pending", "active"),  # _run_approve_side_effects (both approval paths)
    ("pending", "draft"),  # _run_reject_side_effects
    ("pending", "halted"),  # halt_plan
    ("active", "halted"),  # halt_plan
    ("active", "completed"),  # _complete_plan via check_completion
    ("halted", "active"),  # resume_plan
    ("halted", "draft"),  # reset_buy_plan_to_draft
    ("cancelled", "draft"),  # reset_buy_plan_to_draft
    ("completed", "active"),  # resource_line backorder reopen
    ("draft", "cancelled"),  # cancel_buy_plan
    ("pending", "cancelled"),  # cancel_buy_plan
    ("active", "cancelled"),  # cancel_buy_plan
    ("halted", "cancelled"),  # cancel_buy_plan
]

_ILLEGAL_EDGES = [
    ("draft", "active"),  # no approval skip
    ("draft", "completed"),
    ("draft", "halted"),
    ("active", "draft"),  # active never reverts to draft directly
    ("active", "pending"),
    ("completed", "draft"),
    ("completed", "pending"),
    ("completed", "cancelled"),  # terminal except the backorder reopen
    ("cancelled", "pending"),
    ("cancelled", "active"),
    ("halted", "completed"),
]


@pytest.mark.parametrize(("old", "new"), _WRITER_EDGES)
def test_every_writer_edge_is_in_the_table(old: str, new: str):
    assert new in ALLOWED_TRANSITIONS[old]
    plan = _Plan(old)
    transition(plan, new)
    assert plan.status == new


@pytest.mark.parametrize(("old", "new"), _ILLEGAL_EDGES)
def test_illegal_edges_raise(old: str, new: str):
    plan = _Plan(old)
    with pytest.raises(ValueError, match="Invalid buy plan transition"):
        transition(plan, new)
    assert plan.status == old  # untouched on refusal


def test_same_status_is_a_noop():
    plan = _Plan("active")
    transition(plan, "active")
    assert plan.status == "active"


def test_accepts_enum_and_str():
    plan = _Plan("draft")
    transition(plan, BuyPlanStatus.PENDING)
    assert plan.status == "pending"
    plan2 = _Plan("draft")
    transition(plan2, "pending")
    assert plan2.status == "pending"


def test_table_covers_every_status():
    """Every live BuyPlanStatus member appears as a from-state (no orphan origins)."""
    assert set(ALLOWED_TRANSITIONS) == {m.value for m in BuyPlanStatus}


def test_writer_frozensets_agree_with_table():
    """The writers' guard sets must stay consistent with the enforced table."""
    # halt_plan: every haltable status has a halted edge.
    for status in HALTABLE_STATUSES:
        assert BuyPlanStatus.HALTED.value in ALLOWED_TRANSITIONS[status]
    # reset_buy_plan_to_draft: every resubmittable status has a draft edge.
    for status in RESUBMITTABLE_STATUSES:
        assert BuyPlanStatus.DRAFT.value in ALLOWED_TRANSITIONS[status]

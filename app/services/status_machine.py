"""status_machine.py — State machine validation for domain entity status transitions.

Purpose: Prevents invalid status transitions on offers, quotes, requisitions,
         and buy plans. Enforces valid state transitions and provides clear error
         messages when invalid transitions are attempted.

Called by: routers/crm/offers.py, routers/crm/quotes.py, routers/crm/buy_plans.py
Depends on: nothing (pure logic)
"""

from loguru import logger

# ── Offer Status Transitions ────────────────────────────────────────────
# Valid transitions: from_status → {allowed to_statuses}
OFFER_TRANSITIONS: dict[str, set[str]] = {
    "pending_review": {"active", "rejected"},
    "active": {"sold", "rejected", "won", "expired"},
    "won": {"sold"},
    "rejected": set(),  # terminal
    "sold": set(),  # terminal
    "expired": {"active"},  # can be reactivated
}

# ── Quote Status Transitions ────────────────────────────────────────────
QUOTE_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"sent", "revised", "won", "lost"},
    "sent": {"revised", "won", "lost"},
    "revised": {"sent", "won", "lost"},
    "won": set(),  # terminal
    "lost": {"draft"},  # can be re-opened
}

# ── Buy Plan Status Transitions ─────────────────────────────────────────
BUY_PLAN_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending", "cancelled"},
    "pending": {"active", "cancelled", "draft"},
    "active": {"completed", "halted", "cancelled"},
    "halted": {"draft", "cancelled"},
    "completed": set(),  # terminal
    "cancelled": {"draft"},  # can be reset
}

# ── Requisition Status Transitions ──────────────────────────────────────
REQUISITION_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"active", "sourcing", "archived", "cancelled"},
    "active": {"sourcing", "offers", "quoting", "won", "lost", "archived", "cancelled"},
    "sourcing": {"active", "offers", "quoting", "won", "lost", "archived", "cancelled"},
    "offers": {"active", "sourcing", "quoting", "won", "lost", "archived", "cancelled"},
    "quoting": {"active", "sourcing", "offers", "won", "lost", "archived", "cancelled"},
    "won": {"active", "archived"},
    "lost": {"active", "archived"},
    "archived": {"active", "draft"},
    "cancelled": {"active", "draft"},
}


def validate_transition(
    entity_type: str,
    current_status: str | None,
    new_status: str,
    *,
    raise_on_invalid: bool = True,
) -> bool:
    """Validate a status transition for a given entity type.

    Returns True if valid, raises ValueError if invalid (when raise_on_invalid=True).
    Unknown current statuses are treated as allowing any transition (with a warning).
    """
    transition_map = {
        "offer": OFFER_TRANSITIONS,
        "quote": QUOTE_TRANSITIONS,
        "buy_plan": BUY_PLAN_TRANSITIONS,
        "requisition": REQUISITION_TRANSITIONS,
    }

    transitions = transition_map.get(entity_type)
    if not transitions:
        logger.warning("Unknown entity type for status validation: {}", entity_type)
        return True

    if current_status == new_status:
        return True  # no-op transition always valid

    if current_status is None or current_status not in transitions:
        logger.warning(
            "Unknown {} status '{}' — allowing transition to '{}'",
            entity_type,
            current_status,
            new_status,
        )
        return True

    allowed = transitions[current_status]
    if new_status not in allowed:
        msg = (
            f"Invalid {entity_type} status transition: '{current_status}' → '{new_status}'. "
            f"Allowed transitions from '{current_status}': {', '.join(sorted(allowed)) or 'none (terminal state)'}"
        )
        if raise_on_invalid:
            raise ValueError(msg)
        logger.warning(msg)
        return False

    return True

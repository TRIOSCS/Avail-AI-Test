"""buyplan_naming.py — Single shared title helper for Buy-Plan deal cards / triage rows.

Purpose: Derive the canonical card/row title used everywhere a buy plan surfaces as an
         actionable tile in the Deal Hub, so the deal board, the SO-approval triage and
         the PO-approval triage all read identically:

             {SalesOrder#} - {Customer} - {Owner} - {Type}

         Type suffix and Owner source differ per surface:
           - Deal (Buy Plan) card      → suffix "BP", Owner = Account Manager (sales owner)
           - Sales-Order approval row  → suffix "SO", Owner = Account Manager (sales owner)
           - PO approval row           → suffix "PO", Owner = the Buyer

         Field sources on the model (see app/models/buy_plan.py):
           - SalesOrder#       BuyPlan.sales_order_number
           - Customer          quote → customer_site → company.name (buyplan_hub._customer_name)
           - Account Manager   BuyPlan.submitted_by (sales owner who submitted the plan)
           - Buyer             BuyPlanLine.buyer (per-line procurement owner)

         Missing values collapse to an em dash so the title never renders ragged
         (e.g. a fresh DRAFT with no SO# yet → "— - Acme - Jordan - BP").

Called by: app/services/buyplan_hub.py (deal-card + supervise triage read models).
Depends on: nothing (pure string assembly — callers pass already-derived display values).
"""

from __future__ import annotations

#: Display placeholder for any absent title field (matches the template em-dash fallback).
_MISSING = "—"

#: The three deal-hub surfaces and their title suffix. Kept here so a card type can never
#: drift from its suffix in one template while the others stay correct.
CARD_KIND_BUY_PLAN = "BP"
CARD_KIND_SALES_ORDER = "SO"
CARD_KIND_PO = "PO"

_VALID_KINDS = frozenset({CARD_KIND_BUY_PLAN, CARD_KIND_SALES_ORDER, CARD_KIND_PO})


def build_card_title(
    *,
    sales_order_number: str | None,
    customer_name: str | None,
    owner_name: str | None,
    kind: str,
) -> str:
    """Return the canonical ``{SO#} - {Customer} - {Owner} - {Type}`` card title.

    Parameters
    ----------
    sales_order_number:
        ``BuyPlan.sales_order_number`` (the TSO). ``None``/blank → em dash.
    customer_name:
        The derived customer display name (``buyplan_hub._customer_name``).
        ``None``/blank → em dash.
    owner_name:
        The owner display name for this surface — the Account Manager
        (``BuyPlan.submitted_by``) for BP/SO cards, or the Buyer
        (``BuyPlanLine.buyer``) for PO cards. ``None``/blank → em dash.
    kind:
        One of ``"BP"`` / ``"SO"`` / ``"PO"`` (use the ``CARD_KIND_*`` constants).
        The suffix is appended verbatim so every surface ends in its type tag.

    Raises
    ------
    ValueError
        If ``kind`` is not one of the three known card types — surfacing a wiring
        mistake loudly rather than rendering an untyped title.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"Unknown card kind {kind!r}. Valid: {sorted(_VALID_KINDS)}")

    so = (sales_order_number or "").strip() or _MISSING
    customer = (customer_name or "").strip() or _MISSING
    owner = (owner_name or "").strip() or _MISSING
    return f"{so} - {customer} - {owner} - {kind}"


#: AI-flag severities worst → least, so the flagged-issue indicator leads with the most
#: urgent reason. Unknown/absent severities sort last (treated as least urgent).
_SEVERITY_RANK: dict[str, int] = {"critical": 0, "warning": 1, "info": 2}


def summarize_top_flag(ai_flags: list[dict] | None) -> dict | None:
    """Return the single most-urgent AI flag so an indicator can state what's wrong.

    The flagged-issue indicator on a buy plan must say the *actual* problem at first
    glance, not just a count. ``ai_flags`` is the ``BuyPlan.ai_flags`` JSON list of
    ``{type, severity, line_id, message}`` dicts produced by
    ``buyplan_builder.generate_ai_flags`` (e.g. ``"Margin 8.50% below 15% threshold"``,
    ``"No buyer assigned for line (reason: unknown)"``).

    Returns the worst flag (critical → warning → info; original order breaks ties)
    as ``{"severity", "message"}``, or ``None`` when there are no flags. The
    ``message`` is the verbatim reason text the flag system recorded.
    """
    if not ai_flags:
        return None
    # min() with a stable key keeps the first flag of the worst severity (ties unchanged).
    worst = min(ai_flags, key=lambda f: _SEVERITY_RANK.get((f or {}).get("severity"), 99))
    return {
        "severity": worst.get("severity"),
        "message": worst.get("message") or "",
    }

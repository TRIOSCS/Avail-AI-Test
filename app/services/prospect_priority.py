"""prospect_priority.py — Buyer-ready prospect ranking and explainability helpers.

Builds a simple, explainable "buyer ready" score for suggested prospects, plus the
signal-tag and contact-summary helpers the prospecting tab renders. This is the
single source of truth for the "buyer ready" definition across the feature.
Called by: routers/htmx_views.py (the prospecting tab)
Depends on: ProspectAccount-style objects with fit/readiness/signals/contact fields
"""


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value) -> list:
    return value if isinstance(value, list) else []


def _pluralize(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


def build_priority_snapshot(prospect) -> dict:
    """Return an explainable buyer-ready snapshot for a prospect card."""
    fit = prospect.fit_score or 0
    readiness = prospect.readiness_score or 0
    signals = _as_dict(prospect.readiness_signals)
    contacts = _as_list(prospect.contacts_preview)
    similar = _as_list(prospect.similar_customers)
    historical = _as_dict(prospect.historical_context)
    enrichment = _as_dict(prospect.enrichment_data)

    score = fit * 0.45 + readiness * 0.55
    reasons: list[str] = []
    proof_points = 0

    intent = _as_dict(signals.get("intent"))
    intent_strength = intent.get("strength")
    if intent_strength == "strong":
        score += 12
        proof_points += 1
        reasons.append("Strong buying intent")
    elif intent_strength == "moderate":
        score += 6
        proof_points += 1
        reasons.append("Moderate buying intent")

    verified_contacts = sum(1 for c in contacts if isinstance(c, dict) and c.get("verified"))
    verified_dms = sum(
        1 for c in contacts if isinstance(c, dict) and c.get("verified") and c.get("seniority") == "decision_maker"
    )
    if verified_dms:
        score += 9 if verified_dms == 1 else 11
        proof_points += 1
        reasons.append(f"{verified_dms} verified {_pluralize(verified_dms, 'decision-maker')}")
    elif verified_contacts >= 2:
        score += 6
        proof_points += 1
        reasons.append(f"{verified_contacts} verified contacts")
    elif verified_contacts == 1:
        score += 3
        proof_points += 1
        reasons.append("1 verified contact")

    warm_intro = _as_dict(enrichment.get("warm_intro"))
    if warm_intro.get("has_warm_intro"):
        warmth = (warm_intro.get("warmth") or "warm").lower()
        if warmth == "hot":
            score += 10
            reasons.append("Warm intro available")
        else:
            score += 6
            reasons.append("Prior relationship to leverage")
        proof_points += 1

    similar_names: list[str] = []
    for item in similar[:2]:
        if isinstance(item, dict):
            name = (item.get("name") or "").strip()
        else:
            name = str(item).strip()
        if name:
            similar_names.append(name)
    if similar_names:
        score += min(6, len(similar_names) * 3)
        proof_points += 1
        reasons.append(f"Similar wins: {', '.join(similar_names)}")

    quote_count = historical.get("quote_count", 0)
    if not isinstance(quote_count, (int, float)):
        quote_count = 0
    bought_before = bool(historical.get("bought_before"))
    quoted_before = bool(historical.get("quoted_before")) or quote_count > 0
    if bought_before:
        score += 8
        proof_points += 1
        reasons.append("Previous Trio customer")
    elif quoted_before:
        score += 4
        proof_points += 1
        reasons.append("Previous Trio quote history")

    hiring = _as_dict(signals.get("hiring"))
    hiring_type = hiring.get("type")
    if hiring_type == "procurement":
        score += 4
        proof_points += 1
        reasons.append("Procurement hiring signal")
    elif hiring_type == "engineering":
        score += 2
        proof_points += 1
        reasons.append("Engineering growth signal")

    if signals.get("new_procurement_hire") is True:
        score += 3
        proof_points += 1
        reasons.append("New procurement hire")

    if (prospect.import_priority or "").strip().lower() == "priority":
        score += 3
        reasons.append("Marked priority")

    if fit >= 75 and readiness >= 55:
        reasons.append("Strong fit/readiness baseline")
    elif fit >= 70:
        reasons.append("Strong ICP fit")
    elif readiness >= 60:
        reasons.append("Strong near-term timing")

    buyer_ready_score = max(0, min(100, int(round(score))))
    is_buyer_ready = buyer_ready_score >= 70 and proof_points >= 1 and fit >= 50 and readiness >= 30

    if not reasons:
        reasons.append("Needs stronger buyer signals")

    return {
        "buyer_ready_score": buyer_ready_score,
        "priority_reasons": reasons[:4],
        "is_buyer_ready": is_buyer_ready,
        "proof_points": proof_points,
    }


def build_signal_tags(signals) -> list[dict]:
    """Turn the raw readiness_signals JSONB into display-ready pills.

    Each tag is {type, label, tone}. tone is "positive" for genuinely buy-now signals
    (strong intent, procurement hiring, events, new hire) and "neutral" otherwise — so
    the UI never paints a weak/unknown signal as a green checkmark (the old detail
    template rendered every truthy key as a positive pill).
    """
    signals = _as_dict(signals)
    tags: list[dict] = []

    intent = _as_dict(signals.get("intent"))
    strength = intent.get("strength")
    if strength in ("strong", "moderate"):
        tags.append(
            {
                "type": "intent",
                "label": f"Intent: {strength}",
                "tone": "positive" if strength == "strong" else "neutral",
            }
        )

    hiring = _as_dict(signals.get("hiring"))
    hiring_type = hiring.get("type")
    if hiring_type and hiring_type != "none":
        tags.append(
            {
                "type": "hiring",
                "label": f"Hiring: {hiring_type.replace('_', ' ')}",
                "tone": "positive" if hiring_type == "procurement" else "neutral",
            }
        )

    event_types = [
        str(e["type"]).replace("_", " ")
        for e in _as_list(signals.get("events"))
        if isinstance(e, dict) and e.get("type")
    ]
    if event_types:
        tags.append({"type": "event", "label": "Events: " + ", ".join(event_types[:3]), "tone": "positive"})

    if signals.get("new_procurement_hire") is True:
        tags.append({"type": "new_hire", "label": "New procurement hire", "tone": "positive"})

    verified = signals.get("contacts_verified_count")
    if isinstance(verified, int) and verified > 0:
        tags.append(
            {"type": "contacts", "label": f"{verified} verified {_pluralize(verified, 'contact')}", "tone": "neutral"}
        )

    return tags


def contacts_summary(contacts) -> dict:
    """Summarize contacts_preview for the card/detail: total, verified, decision-
    makers."""
    contacts = _as_list(contacts)
    verified = sum(1 for c in contacts if isinstance(c, dict) and c.get("verified"))
    decision_makers = sum(
        1 for c in contacts if isinstance(c, dict) and c.get("verified") and c.get("seniority") == "decision_maker"
    )
    return {"total": len(contacts), "verified": verified, "decision_makers": decision_makers}

"""Prospect scoring service — pure Python ICP fit + readiness calculators.

Deterministic scoring: same input = same output. No external API calls.
Missing data = neutral (mid-range) scores, never zero.
"""

from datetime import UTC, datetime

from loguru import logger

# ── ICP Segment Definitions ──────────────────────────────────────────

ICP_SEGMENTS = {
    "aerospace_defense": {
        "name": "Aerospace & Defense",
        "weight": 0.30,
        "naics_codes": ["336412", "336413"],
        "naics_prefixes": ["3364"],
        "keywords": [
            "aerospace",
            "defense",
            "avionics",
            "avionic",
            "mil-spec",
            "military",
            "satellite",
            "space",
            "airframe",
            "missile",
            "radar",
        ],
    },
    "service_supply_chain": {
        "name": "Service Supply Chain / Installed Base",
        "weight": 0.30,
        "naics_codes": ["334513", "333314", "334510"],
        "naics_prefixes": ["3345", "3333"],
        "keywords": [
            "medical",
            "industrial",
            "instrument",
            "control instrument",
            "measuring",
            "installed base",
            "capital equipment",
            "field service",
            "mro",
            "repair house",
        ],
    },
    "ems_electronics": {
        "name": "Electronics Manufacturing / EMS",
        "weight": 0.25,
        "naics_codes": ["334418", "334417", "334112"],
        "naics_prefixes": ["3344", "3341"],
        "keywords": [
            "ems",
            "electronics manufacturing",
            "pcb",
            "printed circuit",
            "semiconductor",
            "contract manufacturer",
            "pcba",
            "smt",
            "electronic component",
            "circuit board",
            "passive",
            "active",
            "server",
            "datacenter",
            "data center",
            "monitor",
            "desktop",
            "notebook",
        ],
    },
    "automotive": {
        "name": "Automotive OEM / Tier 1",
        "weight": 0.15,
        "naics_codes": ["336310", "336360"],
        "naics_prefixes": ["3363"],
        "keywords": [
            "automotive",
            "vehicle",
            "oem",
            "tier 1",
            "tier1",
            "powertrain",
            "adas",
            "electrification",
            "ev",
        ],
    },
}

ALL_NAICS_CODES = [code for seg in ICP_SEGMENTS.values() for code in seg["naics_codes"]]
ALL_NAICS_4DIGIT = {code[:4] for code in ALL_NAICS_CODES}
ALL_NAICS_3DIGIT = {code[:3] for code in ALL_NAICS_CODES}

# ── Fit Score Weights ────────────────────────────────────────────────

FIT_WEIGHT_INDUSTRY = 30
FIT_WEIGHT_SIZE = 20
FIT_WEIGHT_PROCUREMENT_STAFF = 15
FIT_WEIGHT_NAICS = 15
FIT_WEIGHT_GEOGRAPHY = 10
FIT_WEIGHT_BROKER_USAGE = 10

# ── Readiness Score Weights ──────────────────────────────────────────

READINESS_WEIGHT_INTENT = 35
READINESS_WEIGHT_EVENTS = 25
READINESS_WEIGHT_HIRING = 20
READINESS_WEIGHT_NEW_HIRE = 10
READINESS_WEIGHT_CONTACTS = 10

# ── Composite Score Weights ──────────────────────────────────────────

COMPOSITE_FIT_WEIGHT = 0.60
COMPOSITE_READINESS_WEIGHT = 0.40

# ── Size Score Brackets ──────────────────────────────────────────────

SIZE_BRACKETS = [
    # (min, max, score) — employee count ranges
    (500, 10000, 20),
    (200, 499, 15),
    (10001, None, 15),  # None means unlimited
    (50, 199, 10),
    (0, 49, 5),
]

# Neutral score when size is unknown
SIZE_NEUTRAL = 10


def _parse_employee_range(emp_range: str | None) -> int | None:
    """Extract a representative employee count from a range string.

    Handles formats: "201-500", "1001-5000", "10001+", "5000", "1,000-5,000"
    """
    if not emp_range:
        return None
    s = emp_range.strip().replace(",", "")
    if "+" in s:
        try:
            return int(s.replace("+", ""))
        except ValueError:
            return None
    if "-" in s:
        parts = s.split("-")
        try:
            lo, hi = int(parts[0]), int(parts[1])
            return (lo + hi) // 2
        except (ValueError, IndexError):
            return None
    try:
        return int(s)
    except ValueError:
        return None


# ── Public API ───────────────────────────────────────────────────────


def match_industry_segment(industry: str | None, naics: str | None) -> tuple[str | None, int]:
    """Match against ICP segments.

    Returns (segment_name, score 0-30).
    """
    if not industry and not naics:
        return None, FIT_WEIGHT_INDUSTRY // 3  # neutral: 10

    best_segment = None
    best_score = 0

    naics_clean = (naics or "").strip()

    for key, seg in ICP_SEGMENTS.items():
        score = 0

        # Exact NAICS match = full points
        if naics_clean and naics_clean in seg["naics_codes"]:
            score = FIT_WEIGHT_INDUSTRY  # 30
        # 4-digit NAICS prefix match
        elif naics_clean and len(naics_clean) >= 4 and naics_clean[:4] in {p[:4] for p in seg["naics_prefixes"]}:
            score = 20
        # Industry keyword match
        elif industry:
            ind_lower = industry.lower()
            if any(kw in ind_lower for kw in seg["keywords"]):
                score = 20

        if score > best_score:
            best_score = score
            best_segment = seg["name"]

    # If no segment matched at all, return neutral
    if best_score == 0:
        return None, FIT_WEIGHT_INDUSTRY // 3  # neutral: 10

    return best_segment, best_score


def score_company_size(employee_range: str | None) -> int:
    """Score based on employee count.

    Returns 0-20.
    """
    count = _parse_employee_range(employee_range)
    if count is None:
        return SIZE_NEUTRAL

    for lo, hi, score in SIZE_BRACKETS:
        if hi is None:
            if count >= lo:
                return score
        elif lo <= count <= hi:
            return score

    return SIZE_NEUTRAL  # pragma: no cover — defensive: brackets cover all non-negative ints


def _fit_factors(prospect_data: dict) -> list[tuple[str, str, int, int]]:
    """The single source of truth for the six ICP fit factors.

    Returns an ordered list of ``(label, detail, score, max)`` — ``label`` is a short
    tag for the breakdown hover, ``detail`` is the human-readable reason prefix (the
    per-factor ``reasoning`` text minus its ``(score/max)`` suffix). ``calculate_fit_score``
    (reasoning + total) and ``calculate_fit_breakdown`` (the hover) both derive from this,
    so the score number and the drivers a hover shows can never drift apart.
    """
    factors: list[tuple[str, str, int, int]] = []

    # 1. Industry/segment match (0-30)
    segment, ind_score = match_industry_segment(
        prospect_data.get("industry"),
        prospect_data.get("naics_code"),
    )
    ind_detail = f"Industry: {segment}" if segment else "Industry: no ICP match"
    factors.append(("Industry", ind_detail, ind_score, FIT_WEIGHT_INDUSTRY))

    # 2. Company size (0-20)
    size_score = score_company_size(prospect_data.get("employee_count_range"))
    emp = prospect_data.get("employee_count_range", "unknown")
    factors.append(("Company size", f"Size: {emp}", size_score, FIT_WEIGHT_SIZE))

    # 3. Has procurement/supply chain staff (0-15)
    has_staff = prospect_data.get("has_procurement_staff")
    if has_staff is True:
        staff_score, staff_detail = FIT_WEIGHT_PROCUREMENT_STAFF, "Procurement staff: yes"
    elif has_staff is False:
        staff_score, staff_detail = 0, "Procurement staff: no"
    else:
        staff_score = FIT_WEIGHT_PROCUREMENT_STAFF // 2  # neutral: 7 (truncated from 7.5)
        staff_detail = "Procurement staff: unknown"
    factors.append(("Procurement staff", staff_detail, staff_score, FIT_WEIGHT_PROCUREMENT_STAFF))

    # 4. NAICS code match (0-15) — separate from industry keyword match
    naics = (prospect_data.get("naics_code") or "").strip()
    if naics and naics in ALL_NAICS_CODES:
        naics_score, naics_detail = FIT_WEIGHT_NAICS, f"NAICS: exact match {naics}"
    elif naics and len(naics) >= 4 and naics[:4] in ALL_NAICS_4DIGIT:
        naics_score, naics_detail = 10, f"NAICS: 4-digit match {naics[:4]}"
    elif naics and len(naics) >= 3 and naics[:3] in ALL_NAICS_3DIGIT:
        naics_score, naics_detail = 5, f"NAICS: 3-digit match {naics[:3]}"
    elif naics:
        naics_score, naics_detail = 0, f"NAICS: no match {naics}"
    else:
        naics_score, naics_detail = FIT_WEIGHT_NAICS // 3, "NAICS: unknown"  # neutral: 5
    factors.append(("NAICS", naics_detail, naics_score, FIT_WEIGHT_NAICS))

    # 5. Geographic fit (0-10)
    region = prospect_data.get("region")
    if region and region.lower() in ("global", "multi-region"):
        geo_score = FIT_WEIGHT_GEOGRAPHY  # 10
    elif region and region.upper() in ("US", "EU", "ASIA"):
        geo_score = 7
    elif region:
        geo_score = 3
    else:
        geo_score = FIT_WEIGHT_GEOGRAPHY // 2  # neutral: 5
    factors.append(("Geography", f"Geography: {region or 'unknown'}", geo_score, FIT_WEIGHT_GEOGRAPHY))

    # 6. Already buys from brokers (0-10)
    uses_brokers = prospect_data.get("uses_brokers")
    if uses_brokers is True:
        broker_score = FIT_WEIGHT_BROKER_USAGE  # 10
    elif uses_brokers is False:
        broker_score = 0
    else:
        broker_score = FIT_WEIGHT_BROKER_USAGE // 2  # neutral: 5
    broker_label = "yes" if uses_brokers is True else "no" if uses_brokers is False else "unknown"
    factors.append(("Broker usage", f"Broker usage: {broker_label}", broker_score, FIT_WEIGHT_BROKER_USAGE))

    return factors


def calculate_fit_score(prospect_data: dict) -> tuple[int, str]:
    """Calculate ICP fit score (0-100).

    Args:
        prospect_data: dict with keys: industry, naics_code, employee_count_range,
            region, has_procurement_staff (bool|None), uses_brokers (bool|None)

    Returns:
        (score, reasoning_text) — score is 0-100, reasoning is human-readable.
    """
    factors = _fit_factors(prospect_data)
    total = sum(score for _label, _detail, score, _max in factors)
    reasoning = "; ".join(f"{detail} ({score}/{maximum})" for _label, detail, score, maximum in factors)
    score = min(100, max(0, total))

    logger.debug("Fit score for {}: {} — {}", prospect_data.get("name", "?"), score, reasoning)

    return score, reasoning


def calculate_fit_breakdown(prospect_data: dict) -> list[tuple[str, int]]:
    """Deterministic ``(label, contribution)`` drivers behind ``calculate_fit_score``.

    Derived from the SAME ``_fit_factors`` computation as the score, so the contributions
    sum to the fit score (which is clamped to 100, unreachable given the weights sum to
    exactly 100). Powers the fit-score hover.
    """
    return [(label, score) for label, _detail, score, _max in _fit_factors(prospect_data)]


def calculate_readiness_score(prospect_data: dict, signals: dict) -> tuple[int, dict]:
    """Calculate buy-now readiness (0-100).

    Args:
        prospect_data: dict with basic prospect info
        signals: dict with keys: intent (dict), events (list), hiring (dict),
            new_procurement_hire (bool|None), contacts_verified_count (int)

    Returns:
        (score, signal_breakdown) — breakdown maps each signal to its contribution.
    """
    breakdown = {}
    total = 0
    signals = signals or {}

    # 1. Active buying intent for components (0-35)
    intent = signals.get("intent", {})
    intent_strength = intent.get("strength", "none") if isinstance(intent, dict) else "none"
    intent_map = {"strong": 35, "moderate": 20, "weak": 10, "none": 0}
    intent_score = intent_map.get(intent_strength, 0)
    # Neutral for missing/empty intent
    if not intent:
        intent_score = READINESS_WEIGHT_INTENT // 3  # 11
    total += intent_score
    breakdown["intent"] = {
        "score": intent_score,
        "max": READINESS_WEIGHT_INTENT,
        "detail": intent_strength if intent else "unknown",
    }

    # 2. Recent company events (0-25)
    # No events = no signal (score stays 0, not neutral — absence of events is informative).
    events = signals.get("events", [])
    event_score = 0
    event_details = []
    if isinstance(events, list):
        for ev in events:
            ev_type = (ev.get("type", "") if isinstance(ev, dict) else "").lower()
            if "funding" in ev_type:
                event_score = max(event_score, 25)
                event_details.append("funding")
            elif "product" in ev_type or "launch" in ev_type:
                event_score = max(event_score, 20)
                event_details.append("new_product")
            elif "expansion" in ev_type or "office" in ev_type:
                event_score = max(event_score, 20)
                event_details.append("expansion")
            elif "acquisition" in ev_type or "m&a" in ev_type or "merger" in ev_type:
                event_score = max(event_score, 15)
                event_details.append("m&a")
    event_score = min(event_score, READINESS_WEIGHT_EVENTS)
    total += event_score
    breakdown["events"] = {
        "score": event_score,
        "max": READINESS_WEIGHT_EVENTS,
        "detail": event_details if event_details else "none",
    }

    # 3. Hiring procurement/engineering (0-20)
    hiring = signals.get("hiring", {})
    if isinstance(hiring, dict):
        hiring_type = hiring.get("type", "none")
    else:
        hiring_type = "none"
    hiring_map = {"procurement": 20, "engineering": 15, "general_growth": 10, "none": 0}
    hiring_score = hiring_map.get(hiring_type, 0)
    if not hiring:
        hiring_score = READINESS_WEIGHT_HIRING // 4  # 5 — low neutral
    total += hiring_score
    breakdown["hiring"] = {
        "score": hiring_score,
        "max": READINESS_WEIGHT_HIRING,
        "detail": hiring_type if hiring else "unknown",
    }

    # 4. New hire in procurement <6 months (0-10)
    new_hire = signals.get("new_procurement_hire")
    if new_hire is True:
        new_hire_score = READINESS_WEIGHT_NEW_HIRE  # 10
    elif new_hire is False:
        new_hire_score = 0
    else:
        new_hire_score = 3  # neutral
    total += new_hire_score
    breakdown["new_procurement_hire"] = {
        "score": new_hire_score,
        "max": READINESS_WEIGHT_NEW_HIRE,
        "detail": "yes" if new_hire is True else "no" if new_hire is False else "unknown",
    }

    # 5. Contact quality (0-10)
    verified = signals.get("contacts_verified_count", 0)
    if not isinstance(verified, int):
        verified = 0
    if verified >= 3:
        contact_score = READINESS_WEIGHT_CONTACTS  # 10
    elif verified >= 1:
        contact_score = 7
    elif signals.get("contacts_unverified_count", 0) > 0:
        contact_score = 3
    else:
        contact_score = 0
    total += contact_score
    breakdown["contacts"] = {
        "score": contact_score,
        "max": READINESS_WEIGHT_CONTACTS,
        "detail": f"{verified} verified",
    }

    score = min(100, max(0, total))

    logger.debug(
        "Readiness score for {}: {} — {}",
        prospect_data.get("name", "?"),
        score,
        breakdown,
    )

    return score, breakdown


# Short hover labels for the five readiness signals (keyed on calculate_readiness_score's
# breakdown dict keys — single source of truth for the readiness drivers).
READINESS_FACTOR_LABELS = {
    "intent": "Buying intent",
    "events": "Company events",
    "hiring": "Hiring",
    "new_procurement_hire": "New procurement hire",
    "contacts": "Contact quality",
}


def calculate_readiness_breakdown(signals: dict) -> list[tuple[str, int]]:
    """Deterministic ``(label, contribution)`` drivers behind the readiness score.

    Reuses ``calculate_readiness_score``'s already-structured breakdown (same inputs,
    same weights — single source of truth), so the contributions sum to the readiness
    score. Powers the readiness-score hover.
    """
    _score, breakdown = calculate_readiness_score({}, signals or {})
    return [
        (READINESS_FACTOR_LABELS.get(key, key.replace("_", " ").title()), part["score"])
        for key, part in breakdown.items()
    ]


def fit_breakdown_for_prospect(prospect) -> list[tuple[str, int]]:
    """Fit-score drivers for a ProspectAccount, rebuilt from its stored firmographics.

    Mirrors how prospect_signals builds ``prospect_data`` at scoring time (industry /
    naics / employee range / region are persisted columns), so the breakdown reconstructs
    the same fit score the row displays. Registered as a Jinja global for the hover.
    """
    return calculate_fit_breakdown(
        {
            "name": prospect.name,
            "industry": prospect.industry,
            "naics_code": prospect.naics_code,
            "employee_count_range": prospect.employee_count_range,
            "region": prospect.region,
        }
    )


def readiness_breakdown_for_prospect(prospect) -> list[tuple[str, int]]:
    """Readiness-score drivers for a ProspectAccount, from its persisted
    readiness_signals.

    Registered as a Jinja global for the hover.
    """
    return calculate_readiness_breakdown(getattr(prospect, "readiness_signals", None) or {})


def classify_readiness(score: int) -> str:
    """Classify readiness into action tiers.

    Returns: "call_now" (70+), "nurture" (40-69), "monitor" (<40)
    """
    if score >= 70:
        return "call_now"
    elif score >= 40:
        return "nurture"
    return "monitor"


def calculate_composite_score(fit: int, readiness: int) -> float:
    """Weighted composite for sort order.

    60% fit, 40% readiness.
    """
    return round(fit * COMPOSITE_FIT_WEIGHT + readiness * COMPOSITE_READINESS_WEIGHT, 2)


def apply_historical_bonus(fit: int, readiness: int, historical_context: dict) -> tuple[int, int]:
    """Apply bonus for Salesforce-imported prospects with Trio interaction history.

    Args:
        fit: current fit score (0-100)
        readiness: current readiness score (0-100)
        historical_context: JSONB data — may include quote_count, bought_before,
            last_activity (ISO date string or year), total_revenue, years_active

    Returns:
        (adjusted_fit, adjusted_readiness) — capped at 100 each.
    """
    if not historical_context:
        return fit, readiness

    fit_bonus = 0
    readiness_bonus = 0

    # Fit bonus: prior Trio relationship
    bought = historical_context.get("bought_before", False)
    quoted = historical_context.get("quoted_before", False)
    quote_count = historical_context.get("quote_count", 0)
    if not isinstance(quote_count, (int, float)):
        quote_count = 0

    # Infer quoted_before from quote_count if not explicitly set
    if quote_count > 0 and not quoted:
        quoted = True

    if bought:
        fit_bonus += 15
    elif quoted:
        fit_bonus += 10

    # Readiness bonus: recency + volume
    last_activity = historical_context.get("last_activity")
    if last_activity:
        try:
            # Accept year or ISO date
            year = int(str(last_activity)[:4])
            # "Recent" = activity within the last ~2 years, computed relative to
            # now so the window slides instead of pinning to a fixed year.
            if year >= datetime.now(UTC).year - 2:
                readiness_bonus += 10
        except (ValueError, TypeError):
            pass

    if quote_count > 20:
        readiness_bonus += 5

    adjusted_fit = min(100, fit + fit_bonus)
    adjusted_readiness = min(100, readiness + readiness_bonus)

    if fit_bonus or readiness_bonus:
        logger.debug(
            "Historical bonus applied: fit +{} ({}->{}), readiness +{} ({}->{}), context={}",
            fit_bonus,
            fit,
            adjusted_fit,
            readiness_bonus,
            readiness,
            adjusted_readiness,
            historical_context,
        )

    return adjusted_fit, adjusted_readiness

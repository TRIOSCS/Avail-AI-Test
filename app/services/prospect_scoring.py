"""Prospect scoring service — pure Python ICP fit + readiness calculators.

Deterministic scoring: same input = same output. No external API calls.
Missing data = neutral (mid-range) scores, never zero.
"""

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

ALL_NAICS_CODES = []
for seg in ICP_SEGMENTS.values():
    ALL_NAICS_CODES.extend(seg["naics_codes"])

ALL_NAICS_4DIGIT = set()
for seg in ICP_SEGMENTS.values():
    for code in seg["naics_codes"]:
        ALL_NAICS_4DIGIT.add(code[:4])

ALL_NAICS_3DIGIT = set()
for seg in ICP_SEGMENTS.values():
    for code in seg["naics_codes"]:
        ALL_NAICS_3DIGIT.add(code[:3])

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
    """Match against ICP segments. Returns (segment_name, score 0-30)."""
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
    """Score based on employee count. Returns 0-20."""
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


def calculate_fit_score(prospect_data: dict) -> tuple[int, str]:
    """Calculate ICP fit score (0-100).

    Args:
        prospect_data: dict with keys: industry, naics_code, employee_count_range,
            region, has_procurement_staff (bool|None), uses_brokers (bool|None)

    Returns:
        (score, reasoning_text) — score is 0-100, reasoning is human-readable.
    """
    reasons = []
    total = 0

    # 1. Industry/segment match (0-30)
    segment, ind_score = match_industry_segment(
        prospect_data.get("industry"),
        prospect_data.get("naics_code"),
    )
    total += ind_score
    if segment:
        reasons.append(f"Industry: {segment} ({ind_score}/{FIT_WEIGHT_INDUSTRY})")
    else:
        reasons.append(f"Industry: no ICP match ({ind_score}/{FIT_WEIGHT_INDUSTRY})")

    # 2. Company size (0-20)
    size_score = score_company_size(prospect_data.get("employee_count_range"))
    total += size_score
    emp = prospect_data.get("employee_count_range", "unknown")
    reasons.append(f"Size: {emp} ({size_score}/{FIT_WEIGHT_SIZE})")

    # 3. Has procurement/supply chain staff (0-15)
    has_staff = prospect_data.get("has_procurement_staff")
    if has_staff is True:
        staff_score = FIT_WEIGHT_PROCUREMENT_STAFF  # 15
        reasons.append(f"Procurement staff: yes ({staff_score}/{FIT_WEIGHT_PROCUREMENT_STAFF})")
    elif has_staff is False:
        staff_score = 0
        reasons.append(f"Procurement staff: no (0/{FIT_WEIGHT_PROCUREMENT_STAFF})")
    else:
        staff_score = FIT_WEIGHT_PROCUREMENT_STAFF // 2  # neutral: 8 (truncated from 7.5)
        reasons.append(f"Procurement staff: unknown ({staff_score}/{FIT_WEIGHT_PROCUREMENT_STAFF})")
    total += staff_score

    # 4. NAICS code match (0-15) — separate from industry keyword match
    naics = (prospect_data.get("naics_code") or "").strip()
    if naics and naics in ALL_NAICS_CODES:
        naics_score = FIT_WEIGHT_NAICS  # 15
        reasons.append(f"NAICS: exact match {naics} ({naics_score}/{FIT_WEIGHT_NAICS})")
    elif naics and len(naics) >= 4 and naics[:4] in ALL_NAICS_4DIGIT:
        naics_score = 10
        reasons.append(f"NAICS: 4-digit match {naics[:4]} ({naics_score}/{FIT_WEIGHT_NAICS})")
    elif naics and len(naics) >= 3 and naics[:3] in ALL_NAICS_3DIGIT:
        naics_score = 5
        reasons.append(f"NAICS: 3-digit match {naics[:3]} ({naics_score}/{FIT_WEIGHT_NAICS})")
    elif naics:
        naics_score = 0
        reasons.append(f"NAICS: no match {naics} (0/{FIT_WEIGHT_NAICS})")
    else:
        naics_score = FIT_WEIGHT_NAICS // 3  # neutral: 5
        reasons.append(f"NAICS: unknown ({naics_score}/{FIT_WEIGHT_NAICS})")
    total += naics_score

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
    total += geo_score
    reasons.append(f"Geography: {region or 'unknown'} ({geo_score}/{FIT_WEIGHT_GEOGRAPHY})")

    # 6. Already buys from brokers (0-10)
    uses_brokers = prospect_data.get("uses_brokers")
    if uses_brokers is True:
        broker_score = FIT_WEIGHT_BROKER_USAGE  # 10
    elif uses_brokers is False:
        broker_score = 0
    else:
        broker_score = FIT_WEIGHT_BROKER_USAGE // 2  # neutral: 5
    total += broker_score
    reasons.append(
        f"Broker usage: {'yes' if uses_brokers is True else 'no' if uses_brokers is False else 'unknown'} "
        f"({broker_score}/{FIT_WEIGHT_BROKER_USAGE})"
    )

    score = min(100, max(0, total))
    reasoning = "; ".join(reasons)

    logger.debug("Fit score for {}: {} — {}", prospect_data.get("name", "?"), score, reasoning)

    return score, reasoning


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
    events = signals.get("events", [])
    event_score = 0
    event_details = []
    if isinstance(events, list):
        for ev in events:
            ev_type = ev.get("type", "") if isinstance(ev, dict) else ""
            if "funding" in ev_type.lower():
                event_score = max(event_score, 25)
                event_details.append("funding")
            elif "product" in ev_type.lower() or "launch" in ev_type.lower():
                event_score = max(event_score, 20)
                event_details.append("new_product")
            elif "expansion" in ev_type.lower() or "office" in ev_type.lower():
                event_score = max(event_score, 20)
                event_details.append("expansion")
            elif "acquisition" in ev_type.lower() or "m&a" in ev_type.lower() or "merger" in ev_type.lower():
                event_score = max(event_score, 15)
                event_details.append("m&a")
    event_score = min(event_score, READINESS_WEIGHT_EVENTS)
    if not events:
        event_score = 0  # no events = no signal (not neutral — absence of events is informative)
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
    """Weighted composite for sort order. 60% fit, 40% readiness."""
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
            # "Recent" = within ~2 years of reference (2026)
            if year >= 2024:
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

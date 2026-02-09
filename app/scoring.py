"""
Scoring Engine — ranks vendor results by 6 weighted factors.

Final Score = (Recency×30 + Quantity×20 + Vendor Reliability×20 +
               Data Completeness×10 + Source Credibility×10 + Price×10) × Penalties

Each factor produces a 0-100 sub-score. Weights are configurable in .env.
"""
import re
import math
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from app.config import get_settings


# --- Helpers ---

def normalize_part_number(pn: str) -> str:
    """LM317-T → LM317T (uppercase, strip separators)"""
    return re.sub(r"[\s\-_./]+", "", pn.upper().strip())


def normalize_vendor_name(name: str) -> str:
    """Acme Electronics, Inc. → acme electronics"""
    n = name.lower().strip()
    n = re.sub(r"\b(inc|llc|ltd|corp|co|gmbh|sa|srl)\.?\b", "", n)
    n = re.sub(r"[^a-z0-9\s]", "", n)
    return re.sub(r"\s+", " ", n).strip()


# --- Source credibility lookup ---

SOURCE_CREDIBILITY = {
    "authorized_api": 100,
    "email_reply": 100,     # direct vendor quote from our RFQ
    "direct_offer": 95,
    "octopart": 85,
    "brokerbin": 75,
    "upload": 70,
    "manual": 65,
    "ebay": 30,
    "alibaba": 20,
}

# --- Penalty multipliers (compound — multiply together) ---

PENALTIES = {
    "suspicious_pricing": 0.70,
    "quality_issues": 0.60,
    "slow_responder": 0.90,
    "no_contact_info": 0.80,
    "counterfeit_history": 0.30,
}


# --- Score breakdown (returned with every result) ---

@dataclass
class ScoreBreakdown:
    recency: float = 0
    quantity: float = 0
    vendor_reliability: float = 0
    data_completeness: float = 0
    source_credibility: float = 0
    price: float = 0
    penalty_multiplier: float = 1.0
    penalty_reasons: list[str] = field(default_factory=list)
    raw_total: float = 0
    final_score: float = 0

    def to_dict(self) -> dict:
        return {
            "components": {
                "recency": round(self.recency, 1),
                "quantity": round(self.quantity, 1),
                "vendor_reliability": round(self.vendor_reliability, 1),
                "data_completeness": round(self.data_completeness, 1),
                "source_credibility": round(self.source_credibility, 1),
                "price": round(self.price, 1),
            },
            "penalty_multiplier": round(self.penalty_multiplier, 2),
            "penalty_reasons": self.penalty_reasons,
            "raw_total": round(self.raw_total, 1),
            "final_score": round(self.final_score, 1),
        }


# --- Individual scoring functions ---

def score_recency(seen_at: Optional[datetime]) -> float:
    """Smooth decay: today=100, 7d≈85, 30d≈55, 90d≈25, 365d+=5"""
    if not seen_at:
        return 5.0
    days = (datetime.now(timezone.utc) - seen_at).total_seconds() / 86400
    if days < 0:
        days = 0
    return max(5.0, 100 * math.exp(-0.012 * days))


def score_quantity(qty: Optional[int], target_qty: Optional[int] = None) -> float:
    if not qty or qty <= 0:
        return 10.0
    if qty >= 100_000:
        base = 100.0
    elif qty >= 10_000:
        base = 85.0
    elif qty >= 1_000:
        base = 70.0
    elif qty >= 100:
        base = 50.0
    elif qty >= 10:
        base = 30.0
    else:
        base = 15.0
    if target_qty and qty >= target_qty:
        base = min(base + 15, 100)
    return base


def score_vendor_reliability(total_outreach, total_responses, total_wins, tier, is_authorized) -> float:
    """Composite: response_rate×40 + win_rate×30 + tier×20 + track_record×10"""
    resp_rate = (total_responses / total_outreach * 100) if total_outreach > 0 else 50.0
    win_rate = (total_wins / total_outreach * 100) if total_outreach > 0 else 50.0

    if is_authorized:
        tier_score = 100.0
    elif tier == 1:
        tier_score = 100.0
    elif tier == 2:
        tier_score = 70.0
    elif tier == 3:
        tier_score = 40.0
    else:
        tier_score = 20.0

    if total_outreach <= 2:
        track = 50.0
    elif total_outreach <= 10:
        track = 70.0
    else:
        track = 90.0

    return resp_rate * 0.40 + win_rate * 0.30 + tier_score * 0.20 + track * 0.10


def score_data_completeness(has_price, has_qty, has_lead_time, has_condition, has_url) -> float:
    fields = [has_price, has_qty, has_lead_time, has_condition, has_url]
    weights = [30, 25, 20, 15, 10]
    return sum(w for f, w in zip(fields, weights) if f)


def score_source_credibility(source_type: str) -> float:
    return float(SOURCE_CREDIBILITY.get(source_type, 40))


def score_price(price: Optional[float], all_prices: list[float]) -> float:
    if not price or price <= 0:
        return 0.0
    valid = [p for p in all_prices if p and p > 0]
    if not valid:
        return 50.0
    mn, mx = min(valid), max(valid)
    if mx == mn:
        return 80.0
    normalized = 1 - (price - mn) / (mx - mn)
    return max(normalized * 90 + 10, 10)


def calculate_penalties(red_flags: list[str], has_email: bool, is_blocked: bool) -> tuple[float, list[str]]:
    if is_blocked:
        return 0.0, ["Vendor is blocked"]
    mult = 1.0
    reasons = []
    for flag in (red_flags or []):
        if flag in PENALTIES:
            mult *= PENALTIES[flag]
            reasons.append(flag)
    if not has_email:
        mult *= PENALTIES["no_contact_info"]
        reasons.append("no_contact_info")
    return round(mult, 3), reasons


# --- Main scoring function ---

def score_sighting(
    seen_at, quantity, price, lead_time_days, condition, source_type, source_url,
    total_outreach, total_responses, total_wins, tier, is_authorized,
    red_flags, has_email, is_blocked,
    all_prices, target_qty=None,
) -> ScoreBreakdown:
    """Score a single vendor sighting. Returns full breakdown."""
    s = get_settings()
    bd = ScoreBreakdown()

    bd.recency = score_recency(seen_at)
    bd.quantity = score_quantity(quantity, target_qty)
    bd.vendor_reliability = score_vendor_reliability(
        total_outreach or 0, total_responses or 0, total_wins or 0, tier or 0, is_authorized or False
    )
    bd.data_completeness = score_data_completeness(
        bool(price), bool(quantity), bool(lead_time_days), bool(condition), bool(source_url)
    )
    bd.source_credibility = score_source_credibility(source_type or "")
    bd.price = score_price(float(price) if price else None, all_prices)

    bd.raw_total = (
        bd.recency * (s.weight_recency / 100) +
        bd.quantity * (s.weight_quantity / 100) +
        bd.vendor_reliability * (s.weight_vendor_reliability / 100) +
        bd.data_completeness * (s.weight_data_completeness / 100) +
        bd.source_credibility * (s.weight_source_credibility / 100) +
        bd.price * (s.weight_price / 100)
    )

    bd.penalty_multiplier, bd.penalty_reasons = calculate_penalties(
        red_flags or [], has_email or False, is_blocked or False
    )

    bd.final_score = max(0, min(100, bd.raw_total * bd.penalty_multiplier))
    return bd

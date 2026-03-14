"""Phase 5 — Contact classification and utility functions.

Classifies contact seniority from job titles, masks emails for preview,
filters personal emails, and detects new hires.

All functions are idempotent. Personal emails (gmail, etc.) are filtered out.

Called by: services/customer_enrichment_service.py, routers/ai.py
Depends on: nothing (pure utility module)
"""

import re
from datetime import datetime, timezone

from loguru import logger

# ── Personal Email Filter ────────────────────────────────────────────

PERSONAL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "aol.com",
    "icloud.com",
    "mail.com",
    "protonmail.com",
    "zoho.com",
    "yandex.com",
    "live.com",
    "msn.com",
    "me.com",
    "qq.com",
    "163.com",
    "126.com",
    "gmx.com",
    "gmx.de",
    "web.de",
    "t-online.de",
    "comcast.net",
    "sbcglobal.net",
    "att.net",
    "verizon.net",
    "cox.net",
}

# ── Seniority Classification ────────────────────────────────────────

DECISION_MAKER_PATTERNS = [
    r"\bvp\b",
    r"\bvice\s+president\b",
    r"\bdirector\b",
    r"\bdir\.?\b",
    r"\bsvp\b",
    r"\bevp\b",
    r"\bc[- ]?suite\b",
    r"\bchief\b",
    r"\bceo\b",
    r"\bcoo\b",
    r"\bcfo\b",
    r"\bcpo\b",
    r"\bcto\b",
    r"\bhead\s+of\b",
    r"\bgm\b",
    r"\bgeneral\s+manager\b",
]

INFLUENCER_PATTERNS = [
    r"\bmanager\b",
    r"\bsenior\b",
    r"\bsr\.?\b",
    r"\blead\b",
    r"\bcommodity\s+manager\b",
    r"\bprincipal\b",
    r"\bteam\s+lead\b",
]

EXECUTOR_PATTERNS = [
    r"\bbuyer\b",
    r"\bpurchasing\s+agent\b",
    r"\bcoordinator\b",
    r"\banalyst\b",
    r"\bspecialist\b",
    r"\bplanner\b",
    r"\bassistant\b",
    r"\bclerk\b",
]


def classify_contact_seniority(title: str) -> str:
    """Classify contact seniority from job title.

    Returns: "decision_maker", "influencer", "executor", or "other".
    """
    if not title:
        return "other"

    t = title.lower().strip()

    # Check decision_maker first (VP/Director outranks Manager)
    for pattern in DECISION_MAKER_PATTERNS:
        if re.search(pattern, t):
            return "decision_maker"

    for pattern in INFLUENCER_PATTERNS:
        if re.search(pattern, t):
            return "influencer"

    for pattern in EXECUTOR_PATTERNS:
        if re.search(pattern, t):
            return "executor"

    return "other"


# ── Email Masking ────────────────────────────────────────────────────


def mask_email(email: str) -> str:
    """Mask an email for contacts_preview display.

    "john.smith@company.com" -> "j***@comp..."
    """
    if not email or "@" not in email:
        return ""

    local, domain = email.split("@", 1)
    masked_local = local[0] + "***" if local else "***"
    masked_domain = domain[:4] + "..." if len(domain) > 4 else domain
    return f"{masked_local}@{masked_domain}"


def _is_personal_email(email: str) -> bool:
    """Check if email is from a personal domain (gmail, etc.)."""
    if not email or "@" not in email:
        return False
    domain = email.split("@", 1)[1].lower()
    return domain in PERSONAL_DOMAINS


# ── New Hire Detection ───────────────────────────────────────────────


def _is_new_hire(started_at: str | None) -> bool:
    """Check if someone started their current role within the last 6 months."""
    if not started_at:
        return False
    try:
        start_date = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        six_months_ago = datetime.now(timezone.utc).replace(month=max(1, datetime.now(timezone.utc).month - 6))
        return start_date >= six_months_ago
    except (ValueError, TypeError):
        return False


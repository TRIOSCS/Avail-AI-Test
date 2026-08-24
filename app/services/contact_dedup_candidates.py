"""contact_dedup_candidates.py — cross-company contact-duplicate finder (survey idea
#15).

The nightly _job_contact_dedup already collapses same-SITE + same-email(case) contacts
(kept, out of scope). This closes the documented gap — CROSS-site/cross-company contact
dupes had zero detection — by surfacing high-precision candidate pairs for HUMAN review
in the Data Ops tab. It NEVER merges: contacts are suggest-only here.

Two high-precision bands (both cross-site, both exclude archived / email-less contacts):
  - "email"       — identical email (case-insensitive), different customer_site → score 100
  - "name+domain" — fuzzy full_name (>= _NAME_THRESHOLD) AND shared email domain,
                    different customer_site → the fuzzy score
Pure name-only cross-company pairs (no shared email/domain) are deliberately NOT surfaced
— too noisy, and the plan's own risk note is "respect same-name-different-company".

Computed live on each Data Ops render (mirrors find_vendor/company_dedup_candidates) — no
storage, so no migration. rapidfuzz token_sort_ratio; grouping by email/domain keeps the
pairwise scan bounded (never a full O(n^2) over the whole table).

Called by: app/routers/htmx/settings.py (_render_data_ops).
Depends on: models.crm (SiteContact/CustomerSite/Company), contact_dedup.normalize_contact_name.
"""

from __future__ import annotations

import itertools
from collections import defaultdict

from loguru import logger
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from app.models.crm import Company, CustomerSite, SiteContact
from app.services.contact_dedup import normalize_contact_name

_NAME_THRESHOLD = 82  # token_sort_ratio floor for the name+domain band
_LIMIT = 30
# Hard cap on the driving query so a huge contacts table never materializes wholesale (the
# sibling SQLite dedup finders cap at 500 the same way). Contacts are far fewer than sightings.
_SCAN_CAP = 20000

# A shared email spread across MANY contacts is a generic inbox (info@, sales@) or a data-
# entry default, not one person — skip such groups (also caps the O(n^2) pairwise work).
_MAX_EMAIL_GROUP = 6
# Likewise cap a single email-domain bucket before the name pairwise scan.
_MAX_DOMAIN_GROUP = 60

# Free/public mailbox providers: a shared domain here means nothing about "same org", and the
# buckets are huge — never use them for the name+domain band (email-exact still applies).
_PUBLIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "protonmail.com",
        "proton.me",
        "gmx.com",
        "gmx.net",
        "zoho.com",
        "yandex.com",
        "mail.com",
        "qq.com",
        "163.com",
        "126.com",
        "sina.com",
        "comcast.net",
        "verizon.net",
        "sbcglobal.net",
    }
)

# Optional scalar fields whose presence signals a more-complete contact (keeper choice).
_COMPLETENESS_FIELDS = ("title", "phone", "linkedin_url", "contact_role", "secondary_email", "secondary_phone")


def _pair(a: dict, b: dict, *, score: int, match: str) -> dict:
    # Keeper = the more-complete record; tie → lower id (stable, older row wins).
    keep = a if (a["completeness"], -a["id"]) >= (b["completeness"], -b["id"]) else b
    return {
        "contact_a": {k: a[k] for k in ("id", "full_name", "email", "company_name", "site_name")},
        "contact_b": {k: b[k] for k in ("id", "full_name", "email", "company_name", "site_name")},
        "score": score,
        "match": match,
        "auto_keep_id": keep["id"],
    }


def find_contact_dedup_candidates(
    db: Session, *, limit: int = _LIMIT, name_threshold: int = _NAME_THRESHOLD
) -> list[dict]:
    """Cross-site contact-duplicate pairs for human review.

    Suggest-only; never merges.
    """
    # Project ONLY the scalar columns the scan needs (not full ORM entities) — this keeps a
    # large contacts table out of the session identity map and sidesteps SiteContact's
    # lazy="joined" reports_to self-join. Bounded by _SCAN_CAP like the sibling finders.
    completeness_cols = [getattr(SiteContact, f) for f in _COMPLETENESS_FIELDS]
    rows = (
        db.query(
            SiteContact.id,
            SiteContact.email,
            SiteContact.full_name,
            CustomerSite.id,
            CustomerSite.site_name,
            Company.name,
            *completeness_cols,
        )
        .join(CustomerSite, SiteContact.customer_site_id == CustomerSite.id)
        .join(Company, CustomerSite.company_id == Company.id)
        .filter(SiteContact.is_archived.is_(False))
        .filter(SiteContact.email.isnot(None), SiteContact.email != "")
        .limit(_SCAN_CAP + 1)
        .all()
    )
    if len(rows) > _SCAN_CAP:
        rows = rows[:_SCAN_CAP]
        logger.warning("contact dedup scan hit the {}-row cap — some contacts were not compared this pass", _SCAN_CAP)

    views: list[dict] = []
    for cid, email, full_name, site_id, site_name, company_name, *comp in rows:
        email_lc = (email or "").strip().lower()
        if "@" not in email_lc:
            continue
        views.append(
            {
                "id": cid,
                "site_id": site_id,
                "company_name": company_name,
                "site_name": site_name,
                "full_name": full_name or "",
                "email": email,
                "email_lc": email_lc,
                "domain": email_lc.rsplit("@", 1)[1],
                "name_norm": normalize_contact_name(full_name or ""),
                "completeness": sum(1 for c in comp if c),
            }
        )

    pairs: list[dict] = []
    seen: set[tuple[int, int]] = set()

    # Band 1 — identical email, different site (strongest).
    by_email: dict[str, list[dict]] = defaultdict(list)
    for v in views:
        by_email[v["email_lc"]].append(v)
    oversized_emails: set[str] = set()
    for email_lc, group in by_email.items():
        if len(group) > _MAX_EMAIL_GROUP:
            oversized_emails.add(email_lc)  # a generic shared inbox — excluded from BOTH bands
            continue
        for a, b in itertools.combinations(group, 2):
            if a["site_id"] == b["site_id"]:
                continue  # same-site same-email is the nightly job's domain
            key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
            if key not in seen:
                seen.add(key)
                pairs.append(_pair(a, b, score=100, match="email"))

    # Band 2 — shared CORPORATE email domain + fuzzy name, different site. Excludes public
    # domains, empty domains, and any shared-inbox address band 1 already rejected.
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for v in views:
        dom = v["domain"]
        if dom and dom not in _PUBLIC_EMAIL_DOMAINS and v["email_lc"] not in oversized_emails:
            by_domain[dom].append(v)
    for group in by_domain.values():
        if len(group) > _MAX_DOMAIN_GROUP:
            continue  # too big to pairwise-scan meaningfully; would be mostly noise
        for a, b in itertools.combinations(group, 2):
            if a["site_id"] == b["site_id"]:
                continue
            key = (min(a["id"], b["id"]), max(a["id"], b["id"]))
            if key in seen:
                continue
            if not a["name_norm"] or not b["name_norm"]:
                continue
            score = int(fuzz.token_sort_ratio(a["name_norm"], b["name_norm"]))
            if score >= name_threshold:
                seen.add(key)
                pairs.append(_pair(a, b, score=score, match="name+domain"))

    # Email band first, then name+domain by descending score; cap.
    pairs.sort(key=lambda p: (0 if p["match"] == "email" else 1, -p["score"]))
    return pairs[:limit]


_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "same_person": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": ["string", "null"]},
    },
    "required": ["same_person"],
}

_AI_SYSTEM = (
    "You judge whether two CRM contact records are the SAME real person listed at two "
    "different companies/sites (e.g. someone who changed jobs, or a duplicate). Weigh the "
    "name, email local-part, and email domain. Same full name at unrelated companies is "
    "often DIFFERENT people — say so. Return same_person, a 0..1 confidence, and a one-line "
    "reason. Never guess high-confidence on a name-only coincidence."
)


async def ai_confirm_same_person(a: dict, b: dict) -> dict:
    """On-demand 'are these the same person?' advisory for one pair.

    Returns {"ok": True, "same_person": bool, "confidence": float, "reason": str} or
    {"ok": False, "message": str} on any AI failure — the caller renders a chip either
    way, never a 500. Interactive: fast tier, single attempt.
    """
    from app.utils.claude_client import claude_structured
    from app.utils.claude_errors import ClaudeError

    prompt = (
        f"Contact A: name={a.get('full_name')!r}, email={a.get('email')!r}, company={a.get('company_name')!r}\n"
        f"Contact B: name={b.get('full_name')!r}, email={b.get('email')!r}, company={b.get('company_name')!r}"
    )
    try:
        result = await claude_structured(
            prompt,
            _AI_SCHEMA,
            system=_AI_SYSTEM,
            model_tier="fast",
            max_tokens=200,
            max_attempts=1,
            cost_bucket="contact_dedup_ai_check",
        )
    except ClaudeError:
        return {"ok": False, "message": "Couldn't reach the AI just now."}
    if not isinstance(result, dict) or "same_person" not in result:
        return {"ok": False, "message": "Couldn't reach the AI just now."}
    try:
        confidence = round(float(result.get("confidence") or 0.0), 2)
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(result["reason"]).strip()[:240] if result.get("reason") else None
    return {"ok": True, "same_person": bool(result["same_person"]), "confidence": confidence, "reason": reason}

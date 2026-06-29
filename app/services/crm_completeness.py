"""CRM completeness scoring — % of key fields populated on a Company / SiteContact.

What: pure functions that score how complete a CRM record is across a fixed set
of key fields, returning {pct, filled, total, missing}. Surfaced as a small badge
on the account header (company) and contact row (contact); the missing-field list
powers the badge tooltip and pairs with the existing Enrich button as the
"enrich to fill" affordance (no new enrichment trigger introduced).

Called by: app/routers/htmx/companies.py (account header badge),
    app/template_env.py (``crm_completeness`` Jinja2 global for the contact row).
Depends on: app/models/crm.Company, SiteContact (read-only, no DB access).
"""

from __future__ import annotations

from typing import Any

# (attribute, human label) — the key fields that define a "complete" record.
# Ordered for the tooltip's missing-field list.
COMPANY_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "Name"),
    ("industry", "Industry"),
    ("website", "Website"),
    ("domain", "Domain"),
    ("phone", "Phone"),
    ("hq_city", "HQ City"),
    ("hq_country", "HQ Country"),
    ("account_type", "Account Type"),
    ("employee_size", "Employees"),
    ("account_owner_id", "Owner"),
)

CONTACT_KEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("full_name", "Name"),
    ("title", "Title"),
    ("email", "Email"),
    ("phone", "Phone"),
    ("contact_role", "Role"),
    ("linkedin_url", "LinkedIn"),
)


def _is_filled(value: Any) -> bool:
    """A field counts as filled when it is non-None and not a blank string."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def completeness(obj: Any, fields: tuple[tuple[str, str], ...]) -> dict[str, Any]:
    """Score *obj* over *fields*.

    Returns {"pct": int 0-100, "filled": int, "total": int, "missing": list[str]}. pct
    is rounded to the nearest whole percent.
    """
    total = len(fields)
    missing: list[str] = []
    filled = 0
    for attr, label in fields:
        if _is_filled(getattr(obj, attr, None)):
            filled += 1
        else:
            missing.append(label)
    pct = round(100 * filled / total) if total else 0
    return {"pct": pct, "filled": filled, "total": total, "missing": missing}


def company_completeness(company: Any) -> dict[str, Any]:
    """Completeness score for a Company over COMPANY_KEY_FIELDS."""
    return completeness(company, COMPANY_KEY_FIELDS)


def contact_completeness(contact: Any) -> dict[str, Any]:
    """Completeness score for a SiteContact over CONTACT_KEY_FIELDS."""
    return completeness(contact, CONTACT_KEY_FIELDS)

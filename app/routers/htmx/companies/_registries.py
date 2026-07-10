"""routers/htmx/companies/_registries.py — shared field registries + pure field-apply
helpers for the CRM company/contact split (P4.3).

Holds state genuinely shared across the split company/contact modules: the
inline-editable field registries (``EDITABLE_ACCOUNT_FIELDS`` /
``EDITABLE_CONTACT_FIELDS`` / ``KNOWN_ACCOUNT_FIELDS`` / ``FIELD_LABELS``), the
canonical buying-role taxonomy (``CANONICAL_ROLES`` / ``_VALID_ROLES``), and the
pure (no-commit) field-apply functions (``apply_company_field`` /
``apply_contact_field`` / ``_recompose_full_name`` / ``_validate_role``) used by
both the create/edit form endpoints and the inline-edit endpoints. Deliberately a
leaf module (mirrors the existing ``._shared`` / ``._shared_tabs`` convention in
``app/routers/htmx/``) — it depends on nothing else in this package, so
``core.py`` and ``detail.py`` can both import from it without a cycle.

Called by: app.routers.htmx.companies (package __init__ re-export), .core, .detail,
    .contacts
Depends on: app.constants, app.models, app.utils.normalization_helpers,
    app.schemas.crm
"""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ....constants import CRM_INDUSTRIES, ContactRole
from ....models import Company, SiteContact
from ....schemas.crm import normalize_website
from ....utils.normalization_helpers import normalize_country, normalize_phone_e164, normalize_us_state

# Canonical buying-role taxonomy — sourced from the ContactRole StrEnum (single
# source of truth in app/constants.py; mirrored as the `roles` Jinja2 global in
# app/template_env.py). Legacy DB values (buyer_po/specifier/ap_payer/logistics/
# exec/technical/decision_maker/operations) remain in the DB but can only be cleared
# via the "— clear —" option; they are not in this set.
CANONICAL_ROLES = tuple(ContactRole)
_VALID_ROLES = frozenset(CANONICAL_ROLES)

# Shared cap for the accounts + contacts bulk-action endpoints (.core.customers_bulk_action,
# .contacts.contacts_bulk_action) — one source of truth for the per-request id-list limit.
BULK_MAX_IDS = 200

# ── Inline-editable field registry (WS1) ────────────────────────────────────
# Each field maps to {label, kind, choices (for select)}. tier/disposition/owner
# have dedicated controls and are excluded. owner inline deferred to WS2.

EDITABLE_ACCOUNT_FIELDS: dict[str, dict] = {
    "industry": {"label": "Industry", "kind": "select", "choices": list(CRM_INDUSTRIES)},
    "phone": {"label": "Phone", "kind": "text"},
    "employee_size": {"label": "Employees", "kind": "text"},
    "credit_terms": {"label": "Credit Terms", "kind": "text"},
    "website": {"label": "Website", "kind": "text"},
    "legal_name": {"label": "Legal Name", "kind": "text"},
    "revenue_range": {"label": "Revenue Range", "kind": "text"},
    "hq_city": {"label": "HQ City", "kind": "text"},
    "hq_state": {"label": "HQ State", "kind": "text"},
    "hq_country": {"label": "HQ Country", "kind": "text"},
    "account_type": {
        "label": "Account Type",
        "kind": "select",
        "choices": ["Customer", "Prospect", "Partner", "Competitor"],
    },
    "domain": {"label": "Domain", "kind": "text"},
    "linkedin_url": {"label": "LinkedIn URL", "kind": "text"},
    "tax_id": {"label": "Tax ID", "kind": "text"},
    "source": {"label": "Source", "kind": "text"},
    "notes": {"label": "Notes", "kind": "text"},
}

EDITABLE_CONTACT_FIELDS: dict[str, dict] = {
    # first_name + last_name replace the old single full_name inline editor.
    # apply_contact_field recomposes full_name when either is saved.
    "first_name": {"label": "First Name", "kind": "text"},
    "last_name": {"label": "Last Name", "kind": "text"},
    "title": {"label": "Title", "kind": "text"},
    "email": {"label": "Email", "kind": "text"},
    "phone": {"label": "Phone", "kind": "text"},
    "secondary_email": {"label": "Secondary Email", "kind": "text"},
    "secondary_phone": {"label": "Secondary Phone", "kind": "text"},
    "wechat_id": {"label": "WeChat ID", "kind": "text"},
    "linkedin_url": {"label": "LinkedIn", "kind": "text"},
    "contact_role": {
        "label": "Role",
        "kind": "select",
        "choices": list(CANONICAL_ROLES),
    },
    # contact_owner_id is intentionally NOT listed here — ownership flows via
    # site → account owner (per-contact picker removed in Phase 1).
}

# Ordered list: (field, label, kind, choices) — used by the detail template to render the
# always-visible known-fields grid. Every field here MUST also be in EDITABLE_ACCOUNT_FIELDS
# so the "Add <field>" affordance has a working edit endpoint behind it.
KNOWN_ACCOUNT_FIELDS: list[tuple[str, str, str, list[str] | None]] = [
    ("legal_name", "Legal Name", "text", None),
    ("website", "Website", "text", None),
    ("domain", "Domain", "text", None),
    ("phone", "Phone", "text", None),
    ("employee_size", "Employees", "text", None),
    ("revenue_range", "Revenue Range", "text", None),
    ("hq_city", "HQ City", "text", None),
    ("hq_state", "HQ State", "text", None),
    ("hq_country", "HQ Country", "text", None),
    ("tax_id", "Tax ID", "text", None),
    ("account_type", "Account Type", "select", ["Customer", "Prospect", "Partner", "Competitor"]),
    ("source", "Source", "text", None),
    ("notes", "Notes", "text", None),
]


# Field-name → human label, for rendering the field-history surfaces (company
# History tab + contact History modal). Merges both inline-edit registries so a
# history row's raw field_name resolves to its display label.
FIELD_LABELS: dict[str, str] = {
    field: meta["label"] for field, meta in {**EDITABLE_ACCOUNT_FIELDS, **EDITABLE_CONTACT_FIELDS}.items()
}


def apply_company_field(company: Company, field: str, value: str) -> None:
    """Apply a single inline-edited account field to *company* (does NOT commit).

    Validates/normalizes each field the same way edit_company does. Raises
    HTTPException(400) for invalid values, HTTPException(404) for unknown field. Called
    by both the inline-edit POST endpoint and edit_company (DRY).
    """
    if field not in EDITABLE_ACCOUNT_FIELDS:
        raise HTTPException(404, f"Unknown editable field: {field!r}")
    v = value.strip()
    if field == "phone":
        company.phone = (normalize_phone_e164(v) or v) if v else None
    elif field == "hq_state":
        company.hq_state = (normalize_us_state(v) or v) if v else None
    elif field == "hq_country":
        company.hq_country = (normalize_country(v) or v) if v else None
    elif field == "account_type":
        choices = EDITABLE_ACCOUNT_FIELDS["account_type"]["choices"]
        if v and v not in choices:
            raise HTTPException(400, f"Invalid account_type '{v}'. Valid: {choices}")
        company.account_type = v or None
    elif field == "industry":
        # Constrained pick-list (CRM_INDUSTRIES). Accept a canonical value, a blank
        # (clear), OR the unchanged current value — the last clause preserves legacy
        # free-text industries on no-op saves while constraining every NEW value.
        choices = EDITABLE_ACCOUNT_FIELDS["industry"]["choices"]
        if v and v not in choices and v != (company.industry or ""):
            raise HTTPException(400, f"Invalid industry '{v}'. Valid: {choices}")
        company.industry = v or None
    elif field == "website":
        # Reuse the Company schema's website validator so the inline-edit + edit_company
        # paths reject bad URLs the same way the create form does.
        try:
            company.website = normalize_website(v)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        setattr(company, field, v or None)
    company.updated_at = datetime.now(UTC)


def _recompose_full_name(contact: SiteContact) -> None:
    """Recompose contact.full_name from first_name + last_name (in-place).

    Rule: full_name is always derived from first_name/last_name when either is written
    via the form or inline-edit path. Direct full_name writers (legacy) leave first/last
    unchanged; this function is NOT called for those paths.
    """
    contact.full_name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or (contact.full_name or "")


def apply_contact_field(
    contact: SiteContact,
    field: str,
    value: str,
    site_id: int,
    db: Session,
) -> None:
    """Apply a single inline-edited contact field to *contact* (does NOT commit).

    first_name / last_name edits recompose full_name automatically. At least one of
    first_name / last_name must be non-empty (enforced here). Raises HTTPException for
    invalid values. Called by both the inline-edit POST endpoint and edit_site_contact
    (DRY).
    """
    if field not in EDITABLE_CONTACT_FIELDS:
        raise HTTPException(404, f"Unknown editable contact field: {field!r}")
    v = value.strip()
    if field in ("first_name", "last_name"):
        setattr(contact, field, v or None)
        # After updating, verify at least one name part remains.
        if not contact.first_name and not contact.last_name:
            raise HTTPException(400, "At least one of first_name or last_name is required")
        _recompose_full_name(contact)
    elif field == "email":
        if v and "@" not in v:
            raise HTTPException(400, "Invalid email address")
        if v:
            dup = (
                db.query(SiteContact)
                .filter(
                    SiteContact.customer_site_id == site_id,
                    sqlfunc.lower(SiteContact.email) == v.lower(),
                    SiteContact.id != contact.id,
                )
                .first()
            )
            if dup:
                raise HTTPException(409, f"Another contact at this site already uses {v}")
        contact.email = v or None
    elif field in ("phone", "secondary_phone"):
        # Normalize to E.164 on save, mirroring the account phone path
        # (apply_company_field) — reuses the shared normalize_phone_e164 util.
        setattr(contact, field, (normalize_phone_e164(v) or v) if v else None)
    elif field == "contact_role":
        contact.contact_role = _validate_role(v)
    else:
        setattr(contact, field, v or None)
    contact.updated_at = datetime.now(UTC)


def _validate_role(role_raw: str) -> str | None:
    """Validate a contact_role value: blank → None, unknown → raises HTTPException 400.

    Used by set_contact_role chip endpoint AND edit_site_contact form endpoint so
    both paths share one source of truth for canonical-role enforcement.
    """
    cleaned = (role_raw or "").strip()
    if not cleaned:
        return None
    if cleaned not in _VALID_ROLES:
        raise HTTPException(400, f"Invalid contact_role '{cleaned}'. Valid: {sorted(_VALID_ROLES)}")
    return cleaned

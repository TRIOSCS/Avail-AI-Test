"""contact_dedup.py — shared normalization for AI/provider "Find Contacts" dedup
against already-saved contacts (ISS-025).

Both the vendor Find Contacts worker (``routers/htmx/vendors._run_vendor_find_contacts``)
and the customer suggested-contacts status route
(``routers/htmx/companies/contacts.contacts_tab_suggested_status``) must drop a freshly
discovered suggestion that duplicates a contact already on file for that entity, so
re-running discovery doesn't keep re-surfacing (or re-persisting) the same person.

Matching rule: a suggestion matches an existing contact if its email equals an existing
contact's email case-insensitively; when the suggestion carries no email, fall back to a
normalized full-name match (lowercase, collapsed whitespace) against existing contacts'
names.

Called by: app.routers.htmx.vendors, app.routers.htmx.companies.contacts
Depends on: nothing (leaf module)
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_contact_email(email: str | None) -> str:
    """Lowercase + strip for case-insensitive email dedup. Empty/None -> ''."""
    return (email or "").strip().lower()


def normalize_contact_name(name: str | None) -> str:
    """Lowercase + collapse whitespace for the no-email name-match fallback."""
    if not name:
        return ""
    return _WHITESPACE_RE.sub(" ", name.strip().lower())


def existing_contact_keys(rows: Iterable[Any]) -> tuple[set[str], set[str]]:
    """Build (existing_emails, existing_names) normalized sets from ORM rows exposing
    ``.email`` / ``.full_name`` (VendorContact, ProspectContact, SiteContact).

    Both sets are populated independently per row (a row with an email still
    contributes its normalized name), since the name fallback must match against
    every existing contact's name, not only email-less ones.
    """
    emails: set[str] = set()
    names: set[str] = set()
    for row in rows:
        email = normalize_contact_email(getattr(row, "email", None))
        if email:
            emails.add(email)
        name = normalize_contact_name(getattr(row, "full_name", None))
        if name:
            names.add(name)
    return emails, names


def is_existing_contact(
    email: str | None,
    full_name: str | None,
    existing_emails: set[str],
    existing_names: set[str],
) -> bool:
    """True if a discovered suggestion (email, full_name) already exists.

    Email match wins when the suggestion has an email; otherwise falls back to the
    normalized full-name match.
    """
    norm_email = normalize_contact_email(email)
    if norm_email:
        return norm_email in existing_emails
    norm_name = normalize_contact_name(full_name)
    return bool(norm_name) and norm_name in existing_names

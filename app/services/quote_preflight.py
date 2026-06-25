"""quote_preflight.py — advisory pre-send checks for customer quotes.

What: ``quote_preflight(db, quote)`` runs deterministic, READ-ONLY checks just before a quote
      email is sent and returns a list of advisory ``PreflightWarning``s. It NEVER blocks the
      send — the send UI surfaces the warnings and the salesperson decides. Three checks:
        1. dnc                — recipient site or matching contact is marked Do-Not-Contact
        2. country_of_origin  — a quoted line's sourced offer has a non-US country of origin
        3. mpn_drift          — a quoted MPN is not one the requisition actually asked for
Called by: app/routers/crm/quotes.py (GET /api/quotes/{id}/preflight; the send/preview UI).
Depends on: models (Quote, CustomerSite, SiteContact, Offer, Requirement),
            app.utils.normalization.normalize_mpn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import CustomerSite, Offer, Quote, Requirement, SiteContact
from app.utils.normalization import normalize_mpn

# Accepted spellings of the United States, reduced to letters-only for comparison.
_US_COUNTRY_KEYS = {
    "US",
    "USA",
    "USOFA",
    "UNITEDSTATES",
    "UNITEDSTATESOFAMERICA",
    "UNITEDSTATESAMERICA",
    "AMERICA",
}

_MAX_LISTED = 5  # cap the MPNs/lines named in a message so the banner stays short


@dataclass(frozen=True)
class PreflightWarning:
    """One advisory finding. ``level`` is always 'warning' — preflight never errors/blocks."""

    code: str  # "dnc" | "country_of_origin" | "mpn_drift"
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "level": "warning", "message": self.message}


def _coo_is_non_us(value: object) -> bool:
    """True if *value* is a non-empty country of origin that is not the United States.

    Accepts ``object`` so a SQLAlchemy column value passes cleanly; coerced to str here.
    """
    key = re.sub(r"[^A-Z]", "", str(value or "").upper())
    return bool(key) and key not in _US_COUNTRY_KEYS


def _quote_line_refs(quote: Quote) -> list[tuple[str | None, int | None]]:
    """Return [(mpn, offer_id), ...] for a quote, preferring the structured quote_lines
    relationship and falling back to the legacy line_items JSON (no offer_id there)."""
    if quote.quote_lines:
        return [(ql.mpn, ql.offer_id) for ql in quote.quote_lines]
    refs: list[tuple[str | None, int | None]] = []
    raw_items = quote.line_items
    items = raw_items if isinstance(raw_items, list) else []
    for item in items:
        if isinstance(item, dict):
            refs.append((item.get("mpn"), item.get("offer_id")))
    return refs


def quote_preflight(db: Session, quote: Quote) -> list[PreflightWarning]:
    """Run the advisory pre-send checks. Pure read; returns [] when nothing is flagged."""
    warnings: list[PreflightWarning] = []

    # 1. Do-Not-Contact recipient — site-level flag and/or a matching DNC contact.
    site = db.get(CustomerSite, quote.customer_site_id) if quote.customer_site_id else None
    if site is not None:
        if site.do_not_contact:
            warnings.append(PreflightWarning("dnc", f"Customer site “{site.site_name}” is marked Do-Not-Contact."))
        recipient = (site.contact_email or "").strip().lower()
        if recipient:
            dnc_contact = (
                db.query(SiteContact)
                .filter(
                    SiteContact.customer_site_id == site.id,
                    func.lower(SiteContact.email) == recipient,
                    SiteContact.do_not_contact.is_(True),
                )
                .first()
            )
            if dnc_contact is not None:
                warnings.append(PreflightWarning("dnc", f"Recipient {site.contact_email} is a Do-Not-Contact contact."))

    line_refs = _quote_line_refs(quote)

    # 2. Non-US country of origin — read from each quoted line's sourced offer.
    non_us: list[tuple[str | None, str]] = []
    for mpn, offer_id in line_refs:
        if not offer_id:
            continue
        offer = db.get(Offer, offer_id)
        if offer is None:
            continue
        coo = offer.country_of_origin
        if _coo_is_non_us(coo):
            non_us.append((mpn, str(coo).strip()))
    if non_us:
        listing = ", ".join(f"{mpn or '—'} ({coo})" for mpn, coo in non_us[:_MAX_LISTED])
        warnings.append(
            PreflightWarning(
                "country_of_origin",
                f"Non-US country of origin on {len(non_us)} line(s): {listing}.",
            )
        )

    # 3. MPN drift — quoted MPNs that aren't any of the requisition's requirement MPNs.
    requirement_keys: set[str] = set()
    if quote.requisition_id:
        for req in db.query(Requirement).filter(Requirement.requisition_id == quote.requisition_id):
            for raw in (req.primary_mpn, req.customer_pn, req.oem_pn):
                key = normalize_mpn(raw)
                if key:
                    requirement_keys.add(key)
    drift: list[str] = []
    if requirement_keys:  # only meaningful when the requisition declares MPNs at all
        for mpn, _offer_id in line_refs:
            key = normalize_mpn(mpn)
            if key and key not in requirement_keys:
                drift.append(mpn or "—")
    if drift:
        listing = ", ".join(drift[:_MAX_LISTED])
        warnings.append(PreflightWarning("mpn_drift", f"{len(drift)} quoted MPN(s) not on the requisition: {listing}."))

    return warnings

"""vendor_reachability.py — "can we actually reach this vendor/buyer card" gates.

Two batched (no N+1) checks reused everywhere a card needs to be filtered down to
ones the RFQ/offer send path could truly contact:

- ``cards_with_resolvable_email`` — does the card have a non-empty VendorContact email?
- ``dnc_emails_for_cards`` — which of those emails are Do-Not-Contact flagged?

Both MIRROR the send-path contact resolution in ``email_service.send_batch_rfq`` /
``routers.sightings`` (send_inquiry, preview_inquiry) exactly, so a card that passes
these gates is always genuinely offerable. Advisory only where noted — the
authoritative skip always stays in the send path itself (TOCTOU guard: a contact can be
flagged DNC after these gates run).

Called by: app.routers.sightings (vendor coverage modal, RFQ preview/send),
    app.services.buyer_affinity_service (resell who-to-offer ranking)
Depends on: app.models.vendors (VendorContact), app.models.crm (SiteContact)
"""

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..models.vendors import VendorContact


def cards_with_resolvable_email(db: Session, card_ids: list[int]) -> set[int]:
    """Card ids for which the send path would resolve a non-empty contact email.

    MIRRORS the send-path contact resolution in sightings_send_inquiry /
    sightings_preview_inquiry EXACTLY: a vendor is reachable iff a VendorContact for its
    card has a non-empty ``email`` (the send path reads ``contact.email`` from
    _best_contacts_by_card; it never consults ``card.emails``). One batched query over
    all representative card ids — no N+1 over groups. Empty input → empty set.
    """
    if not card_ids:
        return set()
    rows = (
        db.query(VendorContact.vendor_card_id)
        .filter(
            VendorContact.vendor_card_id.in_(card_ids),
            VendorContact.email.isnot(None),
            VendorContact.email != "",
        )
        .distinct()
        .all()
    )
    return {cid for (cid,) in rows}


def dnc_emails_for_cards(db: Session, card_ids: list[int]) -> set[str]:
    """Return the lowercased email addresses (from VendorContact) that will be DNC-
    skipped by send_batch_rfq for the given vendor card ids.

    Mirrors the send-time DNC check in email_service.send_batch_rfq (line ~181):
    join VendorContact → SiteContact by func.lower(email), filtered on
    SiteContact.do_not_contact.is_(True). Uses func.lower on BOTH sides so the
    advisory set is consistent with the case-insensitive send-time check.

    Returns a set of lowercased emails — the caller compares contact.email.lower()
    against this set. Advisory only; the authoritative skip stays in send_batch_rfq
    (TOCTOU guard — a SiteContact can be flagged after the modal opens).

    Called by: sightings_vendor_modal, sightings_preview_inquiry.
    """
    if not card_ids:
        return set()

    from ..models.crm import SiteContact

    rows = (
        db.query(VendorContact.email)
        .join(
            SiteContact,
            sqlfunc.lower(VendorContact.email) == sqlfunc.lower(SiteContact.email),
        )
        .filter(
            VendorContact.vendor_card_id.in_(card_ids),
            VendorContact.email.isnot(None),
            VendorContact.email != "",
            SiteContact.do_not_contact.is_(True),
        )
        .distinct()
        .all()
    )
    return {email.lower() for (email,) in rows}

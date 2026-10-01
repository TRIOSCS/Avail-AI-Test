"""part_report_service.py — the DB-backed sections of the one-page Search report.

What: given the searched part number plus optional substitutes, assemble the three
      history sections of the report:
        • posted_before — vendors who posted the part before (Sighting rows grouped per
                          vendor, latest first; the resell mirror's synthetic rows excluded)
        • offers        — offers / quotes vendors sent us (Offer rows, latest first)
        • who_to_call   — the people to phone or email for those vendors (VendorCard +
                          VendorContact, falling back to the contact on the posting)
      plus `parse_substitutes` / `resolve_parts`, the shared PN-list helpers every
      report section endpoint uses.
Called by: routers/part_dossier.py (report section endpoints), routers/htmx/search_views.py.
Depends on: MaterialCard (via material_card_service.get_live_card_by_key), Sighting, Offer,
            VendorCard, VendorContact, vendor_utils.normalize_vendor_name,
            excess_mirror.mirror_sighting_filter, utils.normalization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models.intelligence import MaterialCard
from ..models.offers import Offer
from ..models.sourcing import Sighting
from ..models.vendors import VendorCard, VendorContact
from ..utils.normalization import normalize_mpn, normalize_mpn_key
from ..vendor_utils import normalize_vendor_name
from .excess_mirror import mirror_sighting_filter
from .material_card_service import get_live_card_by_key

MAX_SUBSTITUTES = 3
POSTED_BEFORE_LIMIT = 50
OFFERS_LIMIT = 100
CONTACTS_LIMIT = 25
# Raw sighting rows scanned per report — a part rarely has more; the cap bounds the
# Python grouping below.
_SIGHTING_SCAN_LIMIT = 500

_SPLIT_RE = re.compile(r"[,;\s]+")


@dataclass
class ReportPart:
    """One searched part number (primary or substitute) and its material card."""

    display: str
    key: str
    card: MaterialCard | None
    is_primary: bool


@dataclass
class VendorPosting:
    """A vendor's posting history for the part — one row per vendor."""

    vendor_name: str
    part: str
    manufacturer: str | None
    last_seen: datetime | None
    first_seen: datetime | None
    times_seen: int
    last_qty: int | None
    last_price: Decimal | None
    currency: str
    sources: list[str] = field(default_factory=list)
    is_authorized: bool = False
    vendor_email: str | None = None
    vendor_phone: str | None = None
    is_unavailable: bool = False


@dataclass
class CallTarget:
    """Who to call for one vendor."""

    vendor_name: str
    vendor_card_id: int | None
    contact_name: str | None
    contact_title: str | None
    email: str | None
    phone: str | None
    last_contact_at: datetime | None
    total_wins: int
    total_outreach: int
    is_blacklisted: bool
    basis: str  # "posting" | "offer" | "posting + offer"


def parse_substitutes(raw: str | None, primary: str, limit: int = MAX_SUBSTITUTES) -> list[str]:
    """Split a free-text substitutes field into display part numbers.

    Accepts commas, semicolons, or whitespace between numbers. Drops blanks, the primary
    part itself, and duplicates (by canonical key); keeps the first ``limit``.
    """
    if not raw:
        return []
    seen = {normalize_mpn_key(primary)}
    out: list[str] = []
    for token in _SPLIT_RE.split(raw):
        display = normalize_mpn(token)
        if not display:
            continue
        key = normalize_mpn_key(display)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(display)
        if len(out) >= limit:
            break
    return out


def resolve_parts(db: Session, mpn: str, subs: list[str]) -> list[ReportPart]:
    """The searched part first, then its substitutes, each with its live card (or
    None)."""
    parts: list[ReportPart] = []
    for i, pn in enumerate([mpn, *subs]):
        display = (normalize_mpn(pn) or pn.strip().upper()).strip()
        if not display:
            continue
        key = normalize_mpn_key(display)
        parts.append(ReportPart(display=display, key=key, card=get_live_card_by_key(db, key), is_primary=(i == 0)))
    return parts


def _card_ids(parts: list[ReportPart]) -> list[int]:
    return [p.card.id for p in parts if p.card is not None]


def _part_label(parts: list[ReportPart], card_id: int | None) -> str:
    for p in parts:
        if p.card is not None and p.card.id == card_id:
            return p.display
    return parts[0].display if parts else ""


def _vendor_key(name: str | None, normalized: str | None = None) -> str:
    return normalized or normalize_vendor_name(name or "") or (name or "").strip().lower()


def posted_before(db: Session, parts: list[ReportPart], limit: int = POSTED_BEFORE_LIMIT) -> list[VendorPosting]:
    """Vendors who posted the part before, newest posting first.

    Groups the part's real Sighting rows per vendor (normalized name). The latest row
    supplies the display name, part, price, qty and contact; earlier rows add to the
    source list and the seen count.
    """
    ids = _card_ids(parts)
    if not ids:
        return []
    rows = db.scalars(
        select(Sighting)
        .where(Sighting.material_card_id.in_(ids), mirror_sighting_filter())
        .order_by(Sighting.created_at.desc().nullslast(), Sighting.id.desc())
        .limit(_SIGHTING_SCAN_LIMIT)
    ).all()
    grouped: dict[str, VendorPosting] = {}
    for s in rows:
        key = _vendor_key(s.vendor_name, s.vendor_name_normalized)
        if not key:
            continue
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = VendorPosting(
                vendor_name=s.vendor_name,
                part=s.mpn_matched or _part_label(parts, s.material_card_id),
                manufacturer=s.manufacturer,
                last_seen=s.created_at,
                first_seen=s.created_at,
                times_seen=1,
                last_qty=s.qty_available,
                last_price=s.unit_price,
                currency=s.currency or "USD",
                sources=[s.source_type] if s.source_type else [],
                is_authorized=bool(s.is_authorized),
                vendor_email=s.vendor_email,
                vendor_phone=s.vendor_phone,
                is_unavailable=bool(s.is_unavailable),
            )
            continue
        existing.times_seen += 1
        if s.created_at is not None and (existing.first_seen is None or s.created_at < existing.first_seen):
            existing.first_seen = s.created_at
        if s.source_type and s.source_type not in existing.sources:
            existing.sources.append(s.source_type)
        existing.is_authorized = existing.is_authorized or bool(s.is_authorized)
        existing.vendor_email = existing.vendor_email or s.vendor_email
        existing.vendor_phone = existing.vendor_phone or s.vendor_phone
        existing.manufacturer = existing.manufacturer or s.manufacturer
    return list(grouped.values())[:limit]


def offers_for_parts(db: Session, parts: list[ReportPart], limit: int = OFFERS_LIMIT) -> list[Offer]:
    """Offers / quotes vendors sent us for the part, newest first."""
    ids = _card_ids(parts)
    if not ids:
        return []
    return list(
        db.scalars(
            select(Offer)
            .where(Offer.material_card_id.in_(ids))
            .order_by(Offer.created_at.desc().nullslast(), Offer.id.desc())
            .limit(limit)
        ).all()
    )


def distinct_vendor_count(offers: list[Offer]) -> int:
    return len({_vendor_key(o.vendor_name, o.vendor_name_normalized) for o in offers if o.vendor_name})


def _best_contact(card: VendorCard) -> VendorContact | None:
    contacts: list[VendorContact] = [c for c in card.vendor_contacts if c.email or c.phone or c.phone_mobile]
    if not contacts:
        return None
    primary = [c for c in contacts if c.is_primary]
    if primary:
        return primary[0]
    return max(contacts, key=lambda c: (c.relationship_score or 0, c.interaction_count or 0, c.id or 0))


def _first(values: list | None) -> str | None:
    if not values:
        return None
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def who_to_call(
    db: Session,
    postings: list[VendorPosting],
    offers: list[Offer],
    limit: int = CONTACTS_LIMIT,
) -> list[CallTarget]:
    """The people to phone or email for every vendor in the posting and offer history.

    Vendors with a vendor card come first (most wins, then most recently contacted),
    each with its best contact; vendors we only know from a posting follow, using the
    contact the posting carried. Vendors with no reachable channel are left out.
    """
    basis: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    posting_by_key: dict[str, VendorPosting] = {}
    for p in postings:
        key = _vendor_key(p.vendor_name)
        if not key:
            continue
        basis.setdefault(key, set()).add("posting")
        names.setdefault(key, p.vendor_name)
        posting_by_key.setdefault(key, p)
    offer_contact: dict[str, Offer] = {}
    for o in offers:
        key = _vendor_key(o.vendor_name, o.vendor_name_normalized)
        if not key:
            continue
        basis.setdefault(key, set()).add("offer")
        names.setdefault(key, o.vendor_name)
        offer_contact.setdefault(key, o)
    if not basis:
        return []

    cards = {
        c.normalized_name: c
        for c in db.scalars(
            select(VendorCard)
            .options(selectinload(VendorCard.vendor_contacts))
            .where(VendorCard.normalized_name.in_(list(basis.keys())))
        ).all()
    }

    def _basis_label(key: str) -> str:
        kinds = basis[key]
        if kinds == {"posting", "offer"}:
            return "posting + offer"
        return "offer" if "offer" in kinds else "posting"

    with_card: list[CallTarget] = []
    without_card: list[CallTarget] = []
    for key, display in names.items():
        card = cards.get(key)
        if card is not None:
            contact = _best_contact(card)
            email = (contact.email if contact else None) or _first(card.emails)
            phone = (contact.phone or contact.phone_mobile) if contact else None
            phone = phone or _first(card.phones)
            if not (email or phone):
                continue
            with_card.append(
                CallTarget(
                    vendor_name=card.display_name or display,
                    vendor_card_id=card.id,
                    contact_name=contact.full_name if contact else None,
                    contact_title=contact.title if contact else None,
                    email=email,
                    phone=phone,
                    last_contact_at=card.last_contact_at,
                    total_wins=card.total_wins or 0,
                    total_outreach=card.total_outreach or 0,
                    is_blacklisted=bool(card.is_blacklisted),
                    basis=_basis_label(key),
                )
            )
            continue
        posting = posting_by_key.get(key)
        email = posting.vendor_email if posting else None
        phone = posting.vendor_phone if posting else None
        if not (email or phone):
            continue
        without_card.append(
            CallTarget(
                vendor_name=display,
                vendor_card_id=None,
                contact_name=None,
                contact_title=None,
                email=email,
                phone=phone,
                last_contact_at=None,
                total_wins=0,
                total_outreach=0,
                is_blacklisted=False,
                basis=_basis_label(key),
            )
        )

    with_card.sort(
        key=lambda t: (
            -t.total_wins,
            -(t.last_contact_at.timestamp() if t.last_contact_at else 0.0),
            t.vendor_name.lower(),
        )
    )
    return (with_card + without_card)[:limit]

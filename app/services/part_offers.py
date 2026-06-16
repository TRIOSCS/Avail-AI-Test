"""Part-centric offer lookup for the sightings Offers tab.

Returns every Offer for a requirement's part number — primary MPN plus
substitutes — regardless of which requirement/requisition it was entered against.

Called by: app/routers/sightings.py (detail view + offers panel re-render).
Depends on: Offer / Requirement / MaterialCard models, MPN normalization utils.
"""

from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requirement
from app.utils.normalization import normalize_mpn, normalize_mpn_key, parse_substitute_mpns


def _part_mpns(requirement: Requirement) -> list[str]:
    """Primary MPN + substitute MPNs as display strings (deduped, non-empty).

    Substitutes may be the canonical list-of-dicts or legacy plain strings; coerce to
    dicts before parsing so a legacy row can't crash the detail page.
    """
    raw_subs = requirement.substitutes or []
    dict_subs = [s if isinstance(s, dict) else {"mpn": s, "manufacturer": ""} for s in raw_subs]
    subs = parse_substitute_mpns(dict_subs, requirement.primary_mpn)
    mpns = [requirement.primary_mpn] + [s["mpn"] for s in subs]
    return [m for m in mpns if m]


def part_offers_for(requirement: Requirement, db: Session) -> list[Offer]:
    """All offers for the requirement's part (primary + substitutes), newest first.

    Matches on MaterialCard id OR normalized_mpn in BOTH normalization forms, because
    the two offer-creation paths historically wrote normalized_mpn differently (dedup
    key vs. display form). Requirement/requisition of origin does not filter the result.
    """
    mpns = _part_mpns(requirement)
    if not mpns:
        return []

    key_mpns = {normalize_mpn_key(m) for m in mpns}

    norm_keys = set(key_mpns)
    for m in mpns:
        disp = normalize_mpn(m)
        if disp:
            norm_keys.add(disp)
    norm_keys.discard("")

    card_ids = {cid for (cid,) in db.query(MaterialCard.id).filter(MaterialCard.normalized_mpn.in_(key_mpns)).all()}

    conds = [Offer.normalized_mpn.in_(norm_keys)]
    if card_ids:
        conds.append(Offer.material_card_id.in_(card_ids))

    return (
        db.query(Offer)
        .options(joinedload(Offer.requisition))
        .filter(or_(*conds))
        .order_by(Offer.created_at.desc())
        .all()
    )

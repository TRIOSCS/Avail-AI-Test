"""buyer_affinity_service.py — who-to-offer ranking, the buyer scorecard, team overlap,
and the don't-forget nudge.

The intelligence layer of Resell Outreach: the inverse of sourcing's vendor coverage
ranking. Where sourcing asks "which VENDORS stock the parts I need to buy?", resell
asks "which BUYERS should I offer this excess TO?". Everything here is BUYER-side and
keyed on the canonical buyer ``vendor_card_id`` (the same "who" Chunk B's
``counterparty_card`` resolves to). It never touches the ``customer_excess`` supply
Sighting mirror — that mirror is the SELL-side surface; this is the OFFER-OUT side.

Four entry points:
  - ``rank_buyers_for``        — tiered who-to-offer suggestions: buyers who bought
    THIS exact material_card (won ExcessOffer history) → buyers active in the
    list's commodity (VendorCard.commodity_tags + their bought-commodity affinity) →
    an engagement tiebreak. Mirrors the SHAPE of sightings'
    ``_coverage_ranked_vendor_rows`` rank tuple, inverted to buyers. Unreachable /
    DNC / blacklisted buyers are filtered the SAME way the RFQ suggestion does
    (reusing the sightings reachability + DNC gates), so a suggested buyer is always
    actually offerable.
  - ``recompute_buyer_score``  — the per-buyer BuyerScore rollup (offers_received,
    wins, avg_bid_pct_of_ask, response_rate, median_response_hours, last_offered_at,
    commodity_affinity), upserted 1:1 on vendor_card_id. Fed from ExcessOffer +
    ExcessOutreach. Recomputed via ``recompute_buyer_score_on_win`` (the offer-win
    hook) and nightly-batch friendly (``recompute_all_buyer_scores``).
  - ``overlap_warning``        — ADVISORY only (never blocks): has a TEAMMATE
    (``submitted_by != owner``) already offered this buyer overlapping lines on this
    list recently? Returns who / when / which lines, or None.
  - ``not_yet_offered_strip``  — the don't-forget nudge: buyers historically active in
    this list's commodities but with NO ExcessOutreach row on THIS list yet.

ADDITIVE: reuses the sightings reachability + DNC gates and the BuyerScore rollup; it
adds no schema and changes no existing signatures.

Called by: routers/resell.py (Chunk D wiring), the nightly scorecard batch
Depends on: models (ExcessOffer/Line, ExcessOutreach, ExcessList/LineItem, BuyerScore,
            VendorCard, MaterialCard, User), services.vendor_reachability
            (cards_with_resolvable_email / dnc_emails_for_cards gates)
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

from loguru import logger
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..constants import ExcessOfferStatus, ExcessOutreachStatus
from ..models import User, VendorCard
from ..models.excess import (
    BuyerScore,
    ExcessLineItem,
    ExcessOffer,
    ExcessOfferLine,
    ExcessOutreach,
)
from ..models.intelligence import MaterialCard
from ..models.vendors import VendorContact
from ..services.vendor_reachability import cards_with_resolvable_email, dnc_emails_for_cards

# Tier ranks (lower = stronger signal), mirroring the coverage-then-engagement bucket
# ordering in _coverage_ranked_vendor_rows but keyed on the BUYER's affinity signal.
_TIER_BOUGHT_PART = 0
_TIER_COMMODITY = 1
_TIER_ENGAGEMENT = 2
_TIER_REASON = {
    _TIER_BOUGHT_PART: "bought_this_part",
    _TIER_COMMODITY: "buys_this_commodity",
    _TIER_ENGAGEMENT: "engagement",
}

_DEFAULT_LIMIT = 20
_DEFAULT_OVERLAP_DAYS = 14
# Outreach statuses that count as "responded" (the buyer engaged at all) for the
# scorecard response_rate — every terminal-or-engaged state past a bare send.
_RESPONDED_STATUSES = {
    ExcessOutreachStatus.RESPONDED,
    ExcessOutreachStatus.BID,
    ExcessOutreachStatus.DECLINED,
    ExcessOutreachStatus.OPENED,
}
# Outreach statuses where the send did NOT actually reach the buyer — a transient
# ``sending`` row, a ``failed`` send, or an ``interrupted`` (orphaned) one. The SINGLE
# source of truth for "this row is not a genuine offer": reused by every downstream reader
# (offered counts, response_rate denominator, last_offered_at, the don't-forget nudge, the
# tracker) so a non-delivered send never inflates a count, dilutes a rate, or strands a
# re-nudgeable buyer.
_NOT_SENT_STATUSES = {
    ExcessOutreachStatus.SENDING,
    ExcessOutreachStatus.FAILED,
    ExcessOutreachStatus.INTERRUPTED,
}


class RankedBuyer(NamedTuple):
    """One who-to-offer-ranked buyer row — the buyer-side analogue of ``RankedVendor``.

    Fields:
    - ``vendor_card_id`` / ``display_name``: the canonical buyer "who".
    - ``last_bid``: the buyer's most recent ExcessOfferLine unit_price (Decimal | None).
    - ``win_rate``: wins ÷ offers_received from the buyer's BuyerScore (0-1 | None when
      no score row / no offers yet).
    - ``last_offered_at``: when we last offered this buyer anything (from BuyerScore).
    - ``rank_reason``: which tier placed the buyer — "bought_this_part" |
      "buys_this_commodity" | "engagement".
    - ``has_contact``: True iff the RFQ send path would resolve a non-DNC email for the
      buyer (mirrors the sightings reachability gate). Unreachable buyers are dropped
      before ranking, so this is always True on a returned row — kept for parity with
      RankedVendor and for the template badge.
    - ``engagement_score``: the buyer card's engagement score (the tiebreak), or None.
    """

    vendor_card_id: int
    display_name: str
    last_bid: Decimal | None
    win_rate: float | None
    last_offered_at: datetime | None
    rank_reason: str
    has_contact: bool = True
    engagement_score: float | None = None


# ═══════════════════════════════════════════════════════════════════════
#  TARGET / GATE HELPERS
# ═══════════════════════════════════════════════════════════════════════


def _target_lines(
    db: Session,
    *,
    excess_list_id: int | None,
    line_item_ids: list[int] | None,
) -> list[ExcessLineItem]:
    """The ExcessLineItems the ranking is scoped to.

    ``line_item_ids`` (explicit lines) wins; else every line on ``excess_list_id``.
    Raises ValueError when neither is supplied — callers must name a target (mirrors
    Chunk B's "callers must name a buyer" discipline).
    """
    if line_item_ids:
        return db.query(ExcessLineItem).filter(ExcessLineItem.id.in_(line_item_ids)).all()
    if excess_list_id is not None:
        return db.query(ExcessLineItem).filter_by(excess_list_id=excess_list_id).all()
    raise ValueError("rank_buyers_for requires excess_list_id or line_item_ids")


def _target_commodities(db: Session, lines: list[ExcessLineItem]) -> set[str]:
    """The canonical commodity keys (MaterialCard.category) of the target lines."""
    card_ids = {li.material_card_id for li in lines if li.material_card_id is not None}
    if not card_ids:
        return set()
    rows = db.query(MaterialCard.category).filter(MaterialCard.id.in_(card_ids)).all()
    return {c for (c,) in rows if c}


def _reachable_card_ids(db: Session, card_ids: list[int]) -> set[int]:
    """Buyer card ids the RFQ send path could actually reach — reachable AND not DNC.

    Reuses the sightings gates verbatim (the SAME "can we reach this card" logic the RFQ
    suggestion applies) so a suggested buyer is always offerable: a card is kept iff it
    has a resolvable VendorContact email AND none of its emails is flagged
    do_not_contact.
    """
    if not card_ids:
        return set()

    reachable = cards_with_resolvable_email(db, card_ids)
    if not reachable:
        return set()
    dnc_emails = dnc_emails_for_cards(db, list(reachable))
    if not dnc_emails:
        return reachable
    # Drop a card all of whose resolvable emails are DNC-flagged.
    rows = (
        db.query(VendorContact.vendor_card_id, VendorContact.email)
        .filter(
            VendorContact.vendor_card_id.in_(list(reachable)),
            VendorContact.email.isnot(None),
            VendorContact.email != "",
        )
        .all()
    )
    non_dnc: set[int] = set()
    for cid, email in rows:
        if email and email.lower() not in dnc_emails:
            non_dnc.add(cid)
    return non_dnc


# ═══════════════════════════════════════════════════════════════════════
#  RANK BUYERS
# ═══════════════════════════════════════════════════════════════════════


def rank_buyers_for(
    db: Session,
    *,
    excess_list_id: int | None = None,
    line_item_ids: list[int] | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> list[RankedBuyer]:
    """Tiered who-to-offer ranking for an excess list (or a line subset).

    Tiers (strongest first), the buyer-side inversion of vendor coverage ranking:
      1. ``bought_this_part`` — the buyer has a WON ExcessOffer whose matched lines
         carry one of the target ``material_card_id``s (the exact-MPN signal).
      2. ``buys_this_commodity`` — the buyer's ``commodity_tags`` overlap the target
         lines' commodities (MaterialCard.category), OR the buyer has a WON offer in
         one of those commodities historically.
      3. ``engagement`` — any other reachable buyer with an engagement score, ranked
         by it (the cold tiebreak so the panel is never empty).

    Rank tuple MIRRORS ``_coverage_ranked_vendor_rows``: ``(tier, not has_contact,
    -engagement, (0, card_id))`` — strongest tier first, contactable first, higher
    engagement first nullslast, then a stable numeric-id tiebreak. Unreachable / DNC /
    blacklisted buyers are filtered BEFORE ranking (reusing the sightings gates), so
    every returned buyer is actually offerable. Capped at ``limit``.

    Each RankedBuyer carries the scorecard facts the suggestion chip renders
    (last_bid, win_rate, last_offered_at) sourced from the buyer's BuyerScore +
    most-recent offer line. Raises ValueError if neither target is supplied.
    """
    lines = _target_lines(db, excess_list_id=excess_list_id, line_item_ids=line_item_ids)
    target_card_ids = {li.material_card_id for li in lines if li.material_card_id is not None}
    target_commodities = _target_commodities(db, lines)

    # ── Tier-1 set: buyers who WON an offer on one of the target material_cards. ──
    bought_part_buyers: set[int] = set()
    if target_card_ids:
        rows = (
            db.query(ExcessOffer.offerer_vendor_card_id)
            .join(ExcessOfferLine, ExcessOfferLine.offer_id == ExcessOffer.id)
            .join(ExcessLineItem, ExcessLineItem.id == ExcessOfferLine.excess_line_item_id)
            .filter(
                ExcessOffer.status == ExcessOfferStatus.WON,
                ExcessOffer.offerer_vendor_card_id.isnot(None),
                ExcessLineItem.material_card_id.in_(target_card_ids),
            )
            .distinct()
            .all()
        )
        bought_part_buyers = {cid for (cid,) in rows}

    # ── Tier-2 set: buyers active in the target commodities (won a commodity offer). ──
    commodity_offer_buyers: set[int] = set()
    if target_commodities:
        rows = (
            db.query(ExcessOffer.offerer_vendor_card_id)
            .join(ExcessOfferLine, ExcessOfferLine.offer_id == ExcessOffer.id)
            .join(ExcessLineItem, ExcessLineItem.id == ExcessOfferLine.excess_line_item_id)
            .join(MaterialCard, MaterialCard.id == ExcessLineItem.material_card_id)
            .filter(
                ExcessOffer.status == ExcessOfferStatus.WON,
                ExcessOffer.offerer_vendor_card_id.isnot(None),
                MaterialCard.category.in_(target_commodities),
            )
            .distinct()
            .all()
        )
        commodity_offer_buyers = {cid for (cid,) in rows}

    # ── Candidate universe: non-blacklisted buyer cards that could PLAUSIBLY rank. ──
    # A card only survives the ranking loop below if it is a Tier-1/2 history buyer, is
    # commodity-tagged, OR carries an engagement score — so we bound the query to exactly
    # that set instead of loading every VendorCard into Python (Item-0). The commodity-tag
    # JSON column is matched in Python (cross-DB safe), but the SQL pre-filter keeps only
    # cards that have *some* tags / score / history, capping the working set.
    history_ids = bought_part_buyers | commodity_offer_buyers
    candidate_filters = [VendorCard.engagement_score.isnot(None), VendorCard.commodity_tags.isnot(None)]
    if history_ids:
        candidate_filters.append(VendorCard.id.in_(list(history_ids)))
    candidates = db.query(VendorCard).filter(VendorCard.is_blacklisted.is_(False), or_(*candidate_filters)).all()

    reachable = _reachable_card_ids(db, [c.id for c in candidates])
    if not reachable:
        return []

    # Scorecard facts (last_offered_at, win_rate) in one batched lookup.
    score_by_card = {
        s.vendor_card_id: s for s in db.query(BuyerScore).filter(BuyerScore.vendor_card_id.in_(list(reachable))).all()
    }

    ranked: list[tuple[int, bool, float, tuple[int, int], RankedBuyer]] = []
    for card in candidates:
        if card.id not in reachable:
            continue

        tags = {str(t).lower() for t in (card.commodity_tags or [])}
        commodity_tagged = bool(tags & {c.lower() for c in target_commodities}) if target_commodities else False

        if card.id in bought_part_buyers:
            tier = _TIER_BOUGHT_PART
        elif card.id in commodity_offer_buyers or commodity_tagged:
            tier = _TIER_COMMODITY
        elif card.engagement_score is not None:
            tier = _TIER_ENGAGEMENT
        else:
            continue  # no part, no commodity, no engagement → nothing to suggest

        score = score_by_card.get(card.id)
        win_rate = None
        last_offered_at = None
        if score is not None:
            if score.offers_received:
                win_rate = round((score.wins or 0) / score.offers_received, 3)
            last_offered_at = score.last_offered_at

        # last_bid is filled AFTER the limit slice in one batched lookup (avoid N+1).
        rb = RankedBuyer(
            vendor_card_id=card.id,
            display_name=card.display_name,
            last_bid=None,
            win_rate=win_rate,
            last_offered_at=last_offered_at,
            rank_reason=_TIER_REASON[tier],
            has_contact=True,
            engagement_score=card.engagement_score,
        )
        engagement = card.engagement_score if card.engagement_score is not None else float("-inf")
        ranked.append((tier, not rb.has_contact, -engagement, (0, card.id), rb))

    ranked.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    top = [t[4] for t in ranked[: max(0, limit)]]

    # Batch the most-recent-bid lookup for ONLY the returned buyers (no per-card N+1).
    last_bids = _last_bids_for(db, [rb.vendor_card_id for rb in top])
    return [rb._replace(last_bid=last_bids.get(rb.vendor_card_id)) for rb in top]


def _last_bids_for(db: Session, vendor_card_ids: list[int]) -> dict[int, Decimal]:
    """Each buyer's most recent priced ExcessOfferLine unit_price, batched.

    One query for the whole set (replaces the per-card ``_last_bid_for`` N+1): we walk
    the priced offer lines newest-first and keep the FIRST one seen per buyer — the same
    "most recent priced bid" each per-card lookup returned. Buyers with no priced line
    are simply absent from the dict (the caller maps a miss to None).
    """
    if not vendor_card_ids:
        return {}
    rows = (
        db.query(ExcessOffer.offerer_vendor_card_id, ExcessOfferLine.unit_price)
        .join(ExcessOfferLine, ExcessOfferLine.offer_id == ExcessOffer.id)
        .filter(
            ExcessOffer.offerer_vendor_card_id.in_(vendor_card_ids),
            ExcessOfferLine.unit_price.isnot(None),
        )
        .order_by(ExcessOffer.created_at.desc(), ExcessOfferLine.id.desc())
        .all()
    )
    out: dict[int, Decimal] = {}
    for card_id, unit_price in rows:
        if card_id is not None and card_id not in out:
            out[card_id] = unit_price
    return out


# ═══════════════════════════════════════════════════════════════════════
#  BUYER SCORECARD ROLLUP
# ═══════════════════════════════════════════════════════════════════════


def recompute_buyer_score(db: Session, vendor_card_id: int) -> BuyerScore:
    """Recompute (and upsert) the per-buyer BuyerScore rollup — the inverse of the
    vendor scorecard.

    Rolls up the buyer's full ExcessOffer + ExcessOutreach history keyed on the
    canonical ``vendor_card_id``:
      - ``offers_received``  — distinct ExcessOffers from this buyer.
      - ``wins``             — those in status ``won``.
      - ``avg_bid_pct_of_ask`` — mean of (line.unit_price ÷ line item asking_price ×
        100) over priced, matched offer lines whose target line carries an asking
        price (None when no comparable pair exists).
      - ``response_rate``    — responded outreach ÷ sent outreach (None when no sends).
      - ``median_response_hours`` — median (responded sent_at → updated_at) gap, when
        derivable (None otherwise).
      - ``last_offered_at``  — max outreach sent_at / created_at.
      - ``commodity_affinity`` — JSON {commodity: count} of the buyer's bought-before
        lines (the signal that seeds the MPN→commodity→engagement ranking tier-2).

    Upserts the single BuyerScore row for the card (1:1 on vendor_card_id), never
    duplicating. Flushes; the caller commits (so the win hook and the nightly batch
    can both batch their commits). Returns the row.

    Denominator note (finding #17, resolved in Phase-5 plan): the ``response_rate``
    denominator counts every GENUINELY-sent outreach — including a manual-log touch
    (``status=SENT`` with ``sent_at=None``, e.g. a phone / teams / marketplace contact),
    which is a real offer we made and must not be dropped. Only ``_NOT_SENT_STATUSES``
    (SENDING / FAILED / INTERRUPTED) are excluded — those never reached the buyer. The
    finding's original ask to exclude ``sent_at``-NULL rows is SUPERSEDED: it would wrongly
    drop real manual touches. (The nightly backstop ``recompute_all_buyer_scores`` reconciles
    every buyer regardless, so a missed on-win/on-send hook cannot leave a row stale.)
    """
    offers = db.query(ExcessOffer).filter(ExcessOffer.offerer_vendor_card_id == vendor_card_id).all()
    offers_received = len(offers)
    wins = sum(1 for o in offers if o.status == ExcessOfferStatus.WON)

    # ── avg_bid_pct_of_ask + commodity_affinity over the buyer's offer lines. ──
    pcts: list[Decimal] = []
    commodity_affinity: dict[str, int] = {}
    if offers:
        offer_ids = [o.id for o in offers]
        line_rows = (
            db.query(ExcessOfferLine.unit_price, ExcessLineItem.asking_price, MaterialCard.category)
            .join(ExcessLineItem, ExcessLineItem.id == ExcessOfferLine.excess_line_item_id)
            .outerjoin(MaterialCard, MaterialCard.id == ExcessLineItem.material_card_id)
            .filter(ExcessOfferLine.offer_id.in_(offer_ids))
            .all()
        )
        for unit_price, asking_price, category in line_rows:
            if unit_price is not None and asking_price:
                pcts.append((Decimal(unit_price) / Decimal(asking_price)) * Decimal(100))
            if category:
                commodity_affinity[category] = commodity_affinity.get(category, 0) + 1
    avg_bid_pct = (sum(pcts) / len(pcts)).quantize(Decimal("0.01")) if pcts else None

    # ── response_rate + median_response_hours + last_offered_at over outreach. ──
    # Only genuinely-sent rows count: a ``sending`` / ``failed`` / ``interrupted`` row
    # never reached the buyer, so it must not dilute the response_rate denominator nor
    # claim a last_offered_at (its ``created_at`` would falsely read as "we offered them").
    outreach = db.query(ExcessOutreach).filter(ExcessOutreach.target_vendor_card_id == vendor_card_id).all()
    outreach = [o for o in outreach if o.status not in _NOT_SENT_STATUSES]
    sent = len(outreach)
    responded = sum(1 for o in outreach if o.status in _RESPONDED_STATUSES)
    response_rate = (Decimal(responded) / Decimal(sent)).quantize(Decimal("0.01")) if sent else None

    response_gaps: list[float] = []
    last_offered_at: datetime | None = None
    for o in outreach:
        # Only sent rows reach here; ``sent_at`` is the true offer time, with ``created_at``
        # the fallback for a manual-log touch (which legitimately has no ``sent_at``).
        stamp = o.sent_at or o.created_at
        if stamp is not None:
            stamp = _aware(stamp)
            if last_offered_at is None or stamp > last_offered_at:
                last_offered_at = stamp
        if o.status in _RESPONDED_STATUSES and o.sent_at and o.updated_at:
            gap = (_aware(o.updated_at) - _aware(o.sent_at)).total_seconds() / 3600
            if gap >= 0:
                response_gaps.append(gap)
    median_response_hours = Decimal(str(round(_median(response_gaps), 2))) if response_gaps else None

    # ── Upsert the single BuyerScore row (1:1 on vendor_card_id). ──
    score = db.query(BuyerScore).filter_by(vendor_card_id=vendor_card_id).first()
    if score is None:
        score = BuyerScore(vendor_card_id=vendor_card_id)
        db.add(score)
    score.offers_received = offers_received
    score.wins = wins
    score.avg_bid_pct_of_ask = avg_bid_pct
    score.response_rate = response_rate
    score.median_response_hours = median_response_hours
    score.last_offered_at = last_offered_at
    score.commodity_affinity = commodity_affinity or None
    db.flush()
    logger.info(
        "Recomputed BuyerScore for card={} (offers={} wins={} resp_rate={})",
        vendor_card_id,
        offers_received,
        wins,
        response_rate,
    )
    return score


def recompute_buyer_score_on_win(db: Session, offer: ExcessOffer) -> BuyerScore | None:
    """Offer-win hook — recompute the buyer's scorecard when an ExcessOffer is won.

    Call this from WHEREVER an ExcessOffer's status flips to ``won`` (the bid-back /
    award path, Chunk E+). No-ops (returns None) for an offer with no canonical buyer
    card. Idempotent (the rollup reads full history), so a double-fire is harmless.
    Does NOT commit — the award path that flips the status owns the transaction.
    """
    if offer.offerer_vendor_card_id is None:
        return None
    return recompute_buyer_score(db, offer.offerer_vendor_card_id)


def recompute_all_buyer_scores(db: Session) -> int:
    """Nightly-batch backstop — recompute every buyer's scorecard.

    Walks every VendorCard that has either an ExcessOffer or an ExcessOutreach against
    it and recomputes its BuyerScore. Commits once. Returns the count recomputed.
    """
    offer_cards = (
        db.query(ExcessOffer.offerer_vendor_card_id).filter(ExcessOffer.offerer_vendor_card_id.isnot(None)).distinct()
    )
    outreach_cards = (
        db.query(ExcessOutreach.target_vendor_card_id)
        .filter(ExcessOutreach.target_vendor_card_id.isnot(None))
        .distinct()
    )
    card_ids = {cid for (cid,) in offer_cards.all()} | {cid for (cid,) in outreach_cards.all()}
    for cid in card_ids:
        recompute_buyer_score(db, cid)
    db.commit()
    logger.info("Nightly buyer-score backstop recomputed {} buyer(s)", len(card_ids))
    return len(card_ids)


# ═══════════════════════════════════════════════════════════════════════
#  TEAM-OVERLAP (ADVISORY)
# ═══════════════════════════════════════════════════════════════════════


def overlap_warning(
    db: Session,
    *,
    excess_list_id: int,
    target_vendor_card_id: int,
    owner_id: int,
    within_days: int = _DEFAULT_OVERLAP_DAYS,
) -> dict | None:
    """Advisory: has a TEAMMATE already offered this buyer this list recently?

    NEVER blocks and never raises on a missing row — purely informational (the user is
    HYBRID-assertive: warn, log the override, proceed). Looks for an ExcessOutreach on
    ``excess_list_id`` to ``target_vendor_card_id`` whose ``submitted_by`` is NOT
    ``owner_id``, whose status is a GENUINELY-sent one (a sending/failed/interrupted touch
    never reached the buyer, so it is not a real prior offer), and whose ``sent_at``
    (falling back to ``created_at``) is within ``within_days``. Returns the MOST RECENT
    such touch as
    ``{by_user_id, by_user_name, when, line_item_ids}`` (line_item_ids unions the
    overlapping teammate touches), or None when there is no recent teammate overlap.
    """
    cutoff = datetime.now(UTC) - timedelta(days=within_days)
    touches = (
        db.query(ExcessOutreach)
        .filter(
            ExcessOutreach.excess_list_id == excess_list_id,
            ExcessOutreach.target_vendor_card_id == target_vendor_card_id,
            ExcessOutreach.submitted_by != owner_id,
            # Only a GENUINELY-sent touch is a real prior offer: a sending/failed/interrupted
            # row never reached the buyer, so it must not warn a teammate off a still-
            # uncontacted buyer (the same not-sent exclusion the nudge/offered readers use).
            ExcessOutreach.status.notin_([s.value for s in _NOT_SENT_STATUSES]),
        )
        .all()
    )
    # Defensive: a row whose sent_at AND created_at are both NULL has no usable timestamp
    # to compare — skip it rather than let _aware(None) raise (the warning is advisory and
    # must never blow up the offer panel).
    recent = [
        t for t in touches if (t.sent_at or t.created_at) is not None and _aware(t.sent_at or t.created_at) >= cutoff
    ]
    if not recent:
        return None

    recent.sort(key=lambda t: _aware(t.sent_at or t.created_at), reverse=True)
    latest = recent[0]
    teammate = db.get(User, latest.submitted_by)
    line_item_ids = sorted({t.excess_line_item_id for t in recent if t.excess_line_item_id is not None})
    return {
        "by_user_id": latest.submitted_by,
        "by_user_name": teammate.name if teammate else None,
        "when": _aware(latest.sent_at or latest.created_at),
        "line_item_ids": line_item_ids,
    }


def overlap_warnings_for(
    db: Session,
    *,
    excess_list_id: int,
    target_vendor_card_ids: list[int],
    owner_id: int,
    within_days: int = _DEFAULT_OVERLAP_DAYS,
) -> dict[int, dict]:
    """Batched :func:`overlap_warning` for many buyers at once (kills the offer-panel
    N+1).

    ``_suggestion_rows`` used to call :func:`overlap_warning` once per ranked buyer — two
    queries each (the ExcessOutreach scan + the teammate-name ``db.get``) — up to ~40 for a
    full panel, SQLite-masked. This does the same work in exactly TWO queries: one
    ExcessOutreach scan across every target card, one batched teammate-name lookup.

    Returns ``{vendor_card_id: {by_user_id, by_user_name, when, line_item_ids}}`` for the
    buyers with a recent teammate overlap; a buyer with none is simply absent (the caller
    maps a miss to ``None``). Same predicate/shape as the per-buyer function: a teammate's
    (``submitted_by != owner_id``) GENUINELY-sent ExcessOutreach on this list (a sending/
    failed/interrupted touch is excluded — it never reached the buyer) to the buyer whose
    ``sent_at`` (else ``created_at``) is within ``within_days``; the most-recent touch wins;
    ``line_item_ids`` unions the overlapping touches.
    """
    if not target_vendor_card_ids:
        return {}
    cutoff = datetime.now(UTC) - timedelta(days=within_days)
    touches = (
        db.query(ExcessOutreach)
        .filter(
            ExcessOutreach.excess_list_id == excess_list_id,
            ExcessOutreach.target_vendor_card_id.in_(list(target_vendor_card_ids)),
            ExcessOutreach.submitted_by != owner_id,
            # Only a GENUINELY-sent touch is a real prior offer: a sending/failed/interrupted
            # row never reached the buyer, so it must not warn a teammate off a still-
            # uncontacted buyer (the same not-sent exclusion the nudge/offered readers use).
            ExcessOutreach.status.notin_([s.value for s in _NOT_SENT_STATUSES]),
        )
        .all()
    )
    by_card: dict[int, list] = {}
    for t in touches:
        stamp = t.sent_at or t.created_at
        # Skip a row with no usable timestamp (advisory — must never blow up the panel).
        if stamp is None or _aware(stamp) < cutoff:
            continue
        by_card.setdefault(t.target_vendor_card_id, []).append(t)
    if not by_card:
        return {}

    submitter_ids = {t.submitted_by for touches_ in by_card.values() for t in touches_}
    names = dict(db.query(User.id, User.name).filter(User.id.in_(list(submitter_ids))).all())

    result: dict[int, dict] = {}
    for card_id, touches_ in by_card.items():
        touches_.sort(key=lambda t: _aware(t.sent_at or t.created_at), reverse=True)
        latest = touches_[0]
        result[card_id] = {
            "by_user_id": latest.submitted_by,
            "by_user_name": names.get(latest.submitted_by),
            "when": _aware(latest.sent_at or latest.created_at),
            "line_item_ids": sorted({t.excess_line_item_id for t in touches_ if t.excess_line_item_id is not None}),
        }
    return result


# ═══════════════════════════════════════════════════════════════════════
#  DON'T-FORGET NUDGE STRIP
# ═══════════════════════════════════════════════════════════════════════


def not_yet_offered_strip(
    db: Session,
    *,
    excess_list_id: int,
    limit: int = _DEFAULT_LIMIT,
) -> list[RankedBuyer]:
    """The don't-forget nudge ("you usually offer this to X/Y") — buyers active in this
    list's commodities who have NO ExcessOutreach row on THIS list yet.

    Ranks the same way ``rank_buyers_for`` does (so the strip and the suggestion panel
    agree on ordering), then SUBTRACTS the buyers already touched on this list — the
    don't-forget nudge surfaces exactly the historical commodity buyers you have not
    yet offered THIS round. Reachable / non-DNC only (reuses the same gate). Returns
    [] when every historical buyer has already been offered.
    """
    ranked = rank_buyers_for(db, excess_list_id=excess_list_id, limit=limit)
    # "Already offered" excludes non-sent rows: a buyer whose only touch on this list is a
    # ``sending`` / ``failed`` / ``interrupted`` row was not really offered, so they stay
    # re-nudgeable (a failed send should surface again, not silently strand the buyer).
    already = {
        cid
        for (cid,) in db.query(ExcessOutreach.target_vendor_card_id)
        .filter(
            ExcessOutreach.excess_list_id == excess_list_id,
            ExcessOutreach.target_vendor_card_id.isnot(None),
            ExcessOutreach.status.notin_([s.value for s in _NOT_SENT_STATUSES]),
        )
        .distinct()
        .all()
    }
    # The nudge is about buyers with a real history (part/commodity), not the cold
    # engagement tier — those are covered by the live suggestion panel, not "usually".
    return [r for r in ranked if r.vendor_card_id not in already and r.rank_reason != "engagement"]


# ═══════════════════════════════════════════════════════════════════════
#  SMALL UTILITIES
# ═══════════════════════════════════════════════════════════════════════


def _aware(dt: datetime) -> datetime:
    """Coerce a naive timestamp (SQLite returns naive) to UTC-aware for comparison."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _median(values: list[float]) -> float:
    """Median of a non-empty list (caller guarantees non-empty)."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2

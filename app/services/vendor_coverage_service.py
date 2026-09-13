"""services/vendor_coverage_service.py — coverage-ranked vendor suggestions.

Moved verbatim out of ``app/routers/sightings.py`` (item 19, 2026-09) — 141 lines of
pure business logic with no HTTP concerns. ``coverage_ranked_vendor_rows`` is the
single source shared by the vendor modal and the affinity endpoint (which must drop
already-suggested vendors computed the SAME way, so it stays self-contained).
``_vss_vendor_card_join`` and ``RankedVendor`` moved alongside it since they exist only
in service of this ranking (``_vss_vendor_card_join`` has a second direct caller in
sightings.py's vendor modal — it imports it from here too, rather than this service
reaching back into the router).

Called by: app/routers/sightings.py (sightings_vendor_modal, buyer_affinity_service
    call sites — see the ``_coverage_ranked_vendor_rows`` alias kept there for the
    existing test suite, which imports/calls it off ``app.routers.sightings``).
Depends on: app.models.vendor_sighting_summary, app.models.vendors,
    app.services.vendor_reachability, app.vendor_utils.
"""

from typing import NamedTuple

from sqlalchemy import and_, or_
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ..models.vendor_sighting_summary import VendorSightingSummary
from ..models.vendors import VendorCard
from ..services.vendor_reachability import cards_with_resolvable_email as _cards_with_resolvable_email
from ..vendor_utils import normalize_vendor_name


def _vss_vendor_card_join():
    """Coalesce join VendorSightingSummary → VendorCard (F10).

    The vendor_card_id FK (indexed ix_vss_vendor_card) is the PRIMARY branch; VSS
    rows with a NULL FK (e.g. summaries rebuilt before the FK backfill ran) fall
    back to the legacy lower(trim(vendor_name)) == normalized_name match so a
    known vendor's coverage never silently disappears. The NULL-FK guard on the
    fallback prevents double-matching FK rows by name. Plain functions only —
    SQLite + PG safe.

    Called by: coverage_ranked_vendor_rows, sightings_vendor_modal (MPN titles).
    """
    return or_(
        VendorSightingSummary.vendor_card_id == VendorCard.id,
        and_(
            VendorSightingSummary.vendor_card_id.is_(None),
            sqlfunc.lower(sqlfunc.trim(VendorSightingSummary.vendor_name)) == VendorCard.normalized_name,
        ),
    )


class RankedVendor(NamedTuple):
    """One coverage-ranked vendor row (see coverage_ranked_vendor_rows).

    Coverage-discovery (2026-06-15): a row may be CARDLESS — ``card is None`` for a
    vendor that has sightings on the selected parts but no matching VendorCard. Such a
    vendor is surfaced for discovery ("who has my parts?") but is NOT RFQ-able, so
    ``has_contact`` is False and the send path would skip it.

    Fields:
    - ``card``: the representative VendorCard for the group, or None when cardless.
    - ``vendor_name``: the deterministic display name — ``card.display_name`` when
      carded, else the lexicographically-min raw VSS ``vendor_name`` in the group.
    - ``covered_count``: distinct selected requirements this vendor has sightings on.
    - ``avg_score``: mean of the non-null VSS scores in the group (None if all null).
    - ``has_contact``: True iff the send path (``sightings_send_inquiry`` /
      ``sightings_preview_inquiry``) would resolve a non-empty email for this vendor —
      i.e. ``card is not None`` AND some VendorContact for that card has a non-empty
      ``email``. Mirrors the send-skip logic EXACTLY so the "no contact" badge never
      lies. (``card.emails`` is deliberately NOT consulted: the send path resolves the
      address only from VendorContact rows.)
    """

    card: VendorCard | None
    vendor_name: str
    covered_count: int
    avg_score: float | None
    has_contact: bool
    lead_time_days: int | None = None
    vendor_score: float | None = None


def coverage_ranked_vendor_rows(db: Session, req_id_list: list[int], excluded: set[str]) -> list[RankedVendor]:
    """Coverage-ranked suggested vendors — the single source shared by the vendor modal
    and the affinity endpoint (which must drop already-suggested vendors computed the
    SAME way, so it stays self-contained).

    Coverage-discovery (2026-06-15): an OUTER join over VendorSightingSummary →
    VendorCard (via _vss_vendor_card_join) plus Python grouping, so a vendor with
    sightings but NO matching card (card=None, "cardless") is surfaced for discovery
    instead of silently dropped. Python grouping sidesteps the GROUP-BY-entity
    SQLite/PG portability seam; VSS is a few hundred rows — trivial.

    Grouping key: ``card.id`` when carded, else ``normalize_vendor_name(vendor_name)``
    (the canonical normalizer — two name variants of one cardless vendor merge, and the
    key matches the exclusion set). Per group: distinct requirement count
    (covered_count), mean of non-null scores (avg_score), a representative card (None if
    all cardless), and a deterministic display name (card.display_name if carded, else
    the lexicographically-min raw vendor_name).

    Drops: blacklisted only when carded; excluded (unavailability) by normalized name
    (cardless = its group key; carded = normalize_vendor_name(display/normalized_name) —
    belt-and-braces re-check kept for legacy suffixed cards). has_contact mirrors the
    send-skip logic (see _cards_with_resolvable_email).

    Rank: covered_count desc, has_contact desc, responsiveness desc nullslast
    (email_health_score when present — "will this vendor answer an RFQ", exactly what
    the composer is choosing for — else engagement_score for vendors never emailed),
    then a stable group-key tiebreak. Capped at 20.
    """
    raw_rows = (
        db.query(VendorSightingSummary, VendorCard)
        .outerjoin(VendorCard, _vss_vendor_card_join())
        .filter(VendorSightingSummary.requirement_id.in_(req_id_list))
        .all()
    )

    # Group in Python by card.id (carded) or normalize_vendor_name(vendor_name) (cardless).
    groups: dict[object, dict] = {}
    for vss, card in raw_rows:
        # Blacklist applies only to carded vendors (cardless rows have no flag).
        if card is not None and card.is_blacklisted:
            continue
        if card is not None:
            key: object = card.id
        else:
            key = normalize_vendor_name(vss.vendor_name or "")
            if not key:
                continue  # un-normalizable cardless name — nothing to suggest
        g = groups.get(key)
        if g is None:
            g = {"card": None, "req_ids": set(), "scores": [], "raw_names": [], "lead_times": []}
            groups[key] = g
        g["req_ids"].add(vss.requirement_id)
        if vss.score is not None:
            g["scores"].append(vss.score)
        if vss.vendor_name:
            g["raw_names"].append(vss.vendor_name)
        if vss.best_lead_time_days is not None:
            g["lead_times"].append(vss.best_lead_time_days)
        if g["card"] is None and card is not None:
            g["card"] = card

    # Fold suffix-mismatch duplicates (F-H1): the SQL fallback join matches NULL-FK rows by
    # raw lower(trim(vendor_name)) == normalized_name, but cardless grouping keys on
    # normalize_vendor_name(vendor_name). A NULL-FK row "Acme Inc" thus does NOT join to
    # card "acme" (the ' inc' suffix survives raw lower(trim)) yet normalizes to "acme" — it
    # would emit a SECOND, cardless "acme" row with split coverage. Merge any cardless group
    # whose key equals a CARDED group's normalize_vendor_name(display) into that carded
    # group BEFORE ranking, so coverage counts union and no duplicate row is emitted; the
    # carded card / has_contact / display win. Two carded groups never collide on this key
    # (each carded group keys on a distinct card.id).
    carded_by_norm: dict[str, object] = {}
    for ck, cg in groups.items():
        c = cg["card"]
        if c is not None:
            carded_by_norm[normalize_vendor_name(c.display_name or c.normalized_name or "")] = ck
    for cardless_key in [k for k, g in groups.items() if g["card"] is None and k in carded_by_norm]:
        src = groups.pop(cardless_key)
        dst = groups[carded_by_norm[cardless_key]]
        dst["req_ids"].update(src["req_ids"])
        dst["scores"].extend(src["scores"])
        dst["raw_names"].extend(src["raw_names"])
        dst["lead_times"].extend(src["lead_times"])

    # has_contact: one batched VendorContact lookup over all representative card ids.
    contactable_card_ids = _cards_with_resolvable_email(db, [g["card"].id for g in groups.values() if g["card"]])

    ranked: list[tuple[int, bool, float, tuple[int, object], RankedVendor]] = []
    for key, g in groups.items():
        card = g["card"]
        excl_key: object
        if card is not None:
            display = card.display_name or card.normalized_name or ""
            excl_key = normalize_vendor_name(display)
        else:
            display = min(g["raw_names"]) if g["raw_names"] else str(key)
            excl_key = key  # cardless group key IS its normalized name
        # Exclusion (unavailability) drop — cardless by group key, carded belt-and-braces.
        if excluded and excl_key in excluded:
            continue
        covered = len(g["req_ids"])
        scores = g["scores"]
        avg_score = (sum(scores) / len(scores)) if scores else None
        has_contact = card is not None and card.id in contactable_card_ids
        lead_times = g["lead_times"]
        lead_time_days = min(lead_times) if lead_times else None
        rv = RankedVendor(
            card=card,
            vendor_name=display,
            covered_count=covered,
            avg_score=avg_score,
            has_contact=has_contact,
            lead_time_days=lead_time_days,
            vendor_score=(card.vendor_score if card is not None else None),
        )
        # Responsiveness key: prefer email_health_score (composite "will this vendor
        # answer an RFQ" from response_analytics, refreshed by batch_update_email_health)
        # and fall back to engagement_score for vendors never emailed (health NULL).
        # Cardless groups have neither → None (sorts last within their bucket).
        responsiveness: float | None = None
        if card is not None:
            responsiveness = card.email_health_score if card.email_health_score is not None else card.engagement_score
        # Stable, deterministic tiebreak (F-L1): carded ties keep NUMERIC card.id order
        # (bucket 0), cardless after (bucket 1, keyed by group-key string). str(key) alone
        # was lexicographic ("10" < "2"), drifting which equally-ranked vendor fell off the
        # cap-20 vs main's numeric id order.
        tiebreak: tuple[int, object] = (0, card.id) if card is not None else (1, str(key))
        # Sort tuple: covered desc, has_contact desc, responsiveness desc nullslast, then tiebreak.
        ranked.append(
            (
                -covered,
                not has_contact,  # False(0) sorts before True(1) → contactable first
                -(responsiveness if responsiveness is not None else float("-inf")),
                tiebreak,
                rv,
            )
        )

    ranked.sort(key=lambda t: (t[0], t[1], t[2], t[3]))
    return [t[4] for t in ranked[:20]]

"""Search package — MPN expansion: primary+substitutes, FRU crosswalk aliases, the per-
MPN 48h connector cooldown, and obsolete-part lookup.

W4.5a split of app/search_service.py — pure structural move (see cache.py header).
"""

from datetime import UTC, datetime, timedelta
from typing import Final

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import FRU_ALIAS_SOURCE
from ..models import MaterialCard, Requirement
from ..services.fru_matrix_service import get_search_aliases
from ..utils.normalization import MAX_SUBSTITUTES, normalize_mpn, normalize_mpn_key


def get_all_pns(req: Requirement) -> list[str]:
    """Primary MPN + substitutes, deduplicated by canonical key.

    Returns display-normalized MPNs (uppercase, no spaces, keeps dashes).
    """
    pns = []
    seen_keys: set[str] = set()
    if req.primary_mpn and req.primary_mpn.strip():
        display = normalize_mpn(req.primary_mpn) or req.primary_mpn.strip()
        key = normalize_mpn_key(display)
        if key:
            pns.append(display)
            seen_keys.add(key)
    for sub in req.substitutes or []:  # type: ignore[union-attr]  # JSON column is a list at instance level
        if isinstance(sub, dict):
            s = (sub.get("mpn") or "").strip()
        else:
            s = str(sub).strip() if sub else ""
        if not s:
            continue
        display = normalize_mpn(s) or s
        key = normalize_mpn_key(display)
        if key and key not in seen_keys:
            pns.append(display)
            seen_keys.add(key)
    return pns


# Cap on FRU-crosswalk aliases injected per requirement as system-derived
# substitutes (optimization plan 2026-06-12 item 2.7). Priority order is
# fru_matrix_service.SEARCH_ALIAS_KINDS: mfg_model, drive_pn, option, ibm_11s.
MAX_FRU_ALIASES: Final[int] = 8


def _expand_fru_aliases(db: Session, req: Requirement) -> list[dict]:
    """New system-derived substitutes from the FRU crosswalk for req's primary MPN.

    Looks up fru_links in both directions (the primary may be the FRU side OR
    the related side — get_search_aliases handles both with one indexed query)
    and returns canonical substitute dicts
    ``{"mpn": ..., "manufacturer": ..., "source": FRU_ALIAS_SOURCE}`` deduped
    against the primary and the existing substitutes, capped at
    MAX_FRU_ALIASES without ever pushing the stored list past MAX_SUBSTITUTES.

    Read-only (safe on the caller's session); durable persistence happens in
    _persist_fru_aliases through its own write session.
    """
    primary = (req.primary_mpn or "").strip()
    if not primary:
        return []
    try:
        candidates = get_search_aliases(db, primary)
    except Exception as e:
        logger.warning("FRU alias lookup failed for {}: {}", primary, e)
        return []
    if not candidates:
        return []
    room = min(MAX_FRU_ALIASES, MAX_SUBSTITUTES - len(req.substitutes or []))
    if room <= 0:
        return []
    taken = {k for k in (normalize_mpn_key(pn) for pn in get_all_pns(req)) if k}
    aliases: list[dict] = []
    for cand in candidates:
        if len(aliases) >= room:
            break
        if not cand.norm or cand.norm in taken:
            continue
        taken.add(cand.norm)
        display = normalize_mpn(cand.mpn) or cand.mpn
        aliases.append({"mpn": display, "manufacturer": cand.manufacturer, "source": FRU_ALIAS_SOURCE})
    return aliases


def _persist_fru_aliases(db: Session, req_id: int, aliases: list[dict]) -> None:
    """Durably append crosswalk aliases to requirements.substitutes.

    Uses its own short-lived write session (same pattern as search_requirement's main
    write path) so it works on BOTH search paths — including the all-cached short-
    circuit, which never opens the main write session — and never dirties the caller's
    session. Re-deduplicates against the freshly loaded row so concurrent searches of
    the same requirement stay idempotent. Best-effort: a failure here must not break the
    search itself.
    """
    from sqlalchemy.orm import sessionmaker

    _WriteSession = sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False, expire_on_commit=False)
    session = _WriteSession()
    try:
        row = session.get(Requirement, req_id)
        if row is None:
            return
        current = list(row.substitutes or [])
        current_keys = {normalize_mpn_key(row.primary_mpn or "")}
        for sub in current:
            raw = (sub.get("mpn") if isinstance(sub, dict) else str(sub or "")) or ""
            key = normalize_mpn_key(raw)
            if key:
                current_keys.add(key)
        fresh = [a for a in aliases if normalize_mpn_key(a["mpn"]) not in current_keys]
        if not fresh:
            return
        # New list object → SQLAlchemy detects the JSON column change.
        row.substitutes = current + fresh
        session.commit()
        logger.info("Req {}: persisted {} FRU crosswalk substitute(s)", req_id, len(fresh))
    except Exception:
        session.rollback()
        logger.warning("FRU alias persistence failed for requirement {}", req_id, exc_info=True)
    finally:
        session.close()


# How long a MaterialCard.last_searched_at "shields" its MPN from being
# re-queried at supplier APIs. Per-MPN, not per-requirement, so two
# requirements that share an MPN don't each burn quota.
MPN_COOLDOWN_HOURS: Final[int] = 48


def _mpn_cooldown_partition(
    db: Session,
    pns: list[str],
    now: datetime | None = None,
) -> tuple[list[str], list[int]]:
    """Split a requirement's MPNs into (to_search, cached_card_ids).

    A display MPN goes into ``to_search`` when its MaterialCard either does
    not exist or has ``last_searched_at`` older than ``MPN_COOLDOWN_HOURS``.
    Otherwise its card id goes into ``cached_card_ids`` so the caller can
    surface existing sightings via material_card_id linkage.

    Lookups use ``normalize_mpn_key`` so case + packaging-suffix variations
    don't escape the cooldown.
    """
    if not pns:
        return [], []

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=MPN_COOLDOWN_HOURS)

    keys_in_order = []
    key_to_display: dict[str, str] = {}
    for pn in pns:
        k = normalize_mpn_key(pn)
        if not k or k in key_to_display:
            continue
        keys_in_order.append(k)
        key_to_display[k] = pn

    cards = db.query(MaterialCard).filter(MaterialCard.normalized_mpn.in_(keys_in_order)).all()
    card_by_key = {c.normalized_mpn: c for c in cards}

    to_search: list[str] = []
    cached_ids: list[int] = []
    for key in keys_in_order:
        card = card_by_key.get(key)
        if card is None or card.last_searched_at is None:
            to_search.append(key_to_display[key])
            continue
        # MaterialCard.last_searched_at uses raw DateTime (not UTCDateTime),
        # so SQLite roundtrips strip tzinfo. Coerce to UTC for comparison.
        last = card.last_searched_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        # >= 48h old → search again (boundary is inclusive on the stale side)
        if last <= cutoff:
            to_search.append(key_to_display[key])
        else:
            cached_ids.append(card.id)
    return to_search, cached_ids


def _any_pn_obsolete(db: Session, pns: list[str]) -> bool:
    """True if any of ``pns`` maps to a MaterialCard marked obsolete.

    ``pns`` are display-form MPNs (uppercase, dashes preserved) as produced by
    ``get_all_pns``, but ``MaterialCard.normalized_mpn`` stores the canonical
    KEY form (``normalize_mpn_key``: lowercase, non-alphanumerics stripped).
    Query with the key form — a raw display-form ``filter_by`` never matches.
    All keys are batched into a single indexed ``.in_()`` query to avoid an N+1.
    """
    keys = [k for k in (normalize_mpn_key(pn) for pn in pns) if k]
    if not keys:
        return False
    return (
        db.query(MaterialCard.id)
        .filter(
            MaterialCard.normalized_mpn.in_(keys),
            MaterialCard.lifecycle_status == "obsolete",
        )
        .first()
        is not None
    )

"""Search package — material-card resolution: vendor history rows, find-or-create card
upsert, inline deterministic enrichment passes, and background enrichment.

W4.5a split of app/search_service.py — pure structural move (see cache.py header).
"""

from datetime import datetime

from loguru import logger
from sqlalchemy.orm import Session

from ..models import MaterialCard, MaterialVendorHistory, Sighting
from ..scoring import classify_lead, confidence_color, explain_lead
from ..services.price_snapshot_service import record_price_snapshot
from ..utils.async_helpers import safe_background_task
from ..utils.normalization import normalize_mpn, normalize_mpn_key
from ..vendor_utils import normalize_vendor_name


def _get_material_history(material_card_ids: list[int], fresh_vendors: set, db: Session) -> list[dict]:
    """All vendor touchpoints from material cards, excluding vendors with fresh
    sightings."""
    if not material_card_ids:
        return []

    cards = (
        db.query(MaterialCard).filter(MaterialCard.id.in_(material_card_ids), MaterialCard.deleted_at.is_(None)).all()
    )
    if not cards:
        return []

    card_map = {c.id: c for c in cards}
    all_vh = db.query(MaterialVendorHistory).filter(MaterialVendorHistory.material_card_id.in_(material_card_ids)).all()

    from ..vendor_utils import normalize_vendor_name as _nvn

    rows = []
    for vh in all_vh:
        vk = _nvn(vh.vendor_name) or vh.vendor_name.lower()
        if vk in fresh_vendors:
            continue
        card = card_map[vh.material_card_id]
        rows.append(
            {
                "vendor_name": vh.vendor_name,
                "mpn_matched": card.display_mpn,
                "manufacturer": vh.last_manufacturer,
                "qty_available": vh.last_qty,
                "unit_price": vh.last_price,
                "currency": vh.last_currency or "USD",
                "source_type": vh.source_type,
                "is_authorized": vh.is_authorized or False,
                "vendor_sku": vh.vendor_sku,
                "first_seen": vh.first_seen,
                "last_seen": vh.last_seen,
                "times_seen": vh.times_seen or 1,
                "material_card_id": card.id,
            }
        )
    if not rows:
        return rows

    # Spec §9: MaterialVendorHistory stores NO score column, so the ONLY score a
    # history row may carry is the newest matching sighting's PERSISTED v2 score —
    # joined here in ONE batched query per (material_card_id, vendor). Pairs with no
    # surviving scored sighting stay metadata-only (persisted_score=None); a score is
    # never re-derived at display time.
    wanted_pairs = {(r["material_card_id"], _nvn(r["vendor_name"]) or r["vendor_name"].lower()) for r in rows}
    scored = (
        db.query(
            Sighting.material_card_id,
            Sighting.vendor_name_normalized,
            Sighting.score,
            Sighting.score_components,
        )
        .filter(
            Sighting.material_card_id.in_(material_card_ids),
            Sighting.vendor_name_normalized.in_({vn for _, vn in wanted_pairs}),
            Sighting.score.isnot(None),
        )
        .order_by(Sighting.created_at.desc())
        .all()
    )
    newest_by_pair: dict[tuple, tuple] = {}
    for mc_id, vn_norm, score, components in scored:
        key = (mc_id, vn_norm)
        if key in wanted_pairs and key not in newest_by_pair:
            newest_by_pair[key] = (score, components)
    for r in rows:
        vk = _nvn(r["vendor_name"]) or r["vendor_name"].lower()
        hit = newest_by_pair.get((r["material_card_id"], vk))
        r["persisted_score"] = hit[0] if hit else None
        r["persisted_score_components"] = hit[1] if hit else None
    return rows


def _history_to_result(h: dict, now: datetime) -> dict:
    last_seen = h["last_seen"]
    age_days = (now - last_seen).days if last_seen else 999

    has_price = h["unit_price"] is not None
    has_qty = h["qty_available"] is not None

    # Spec §9: display reads the persisted v2 score joined by _get_material_history
    # (the newest matching sighting's sightings.score). No surviving scored sighting →
    # metadata-only row: score 0 / no confidence chip. The old age-band formula and
    # the score_unified "historical" branch (which gave the SAME row two disagreeing
    # numbers) both died in the scoring cut.
    persisted = h.get("persisted_score")
    score = round(float(persisted), 1) if persisted is not None else 0
    confidence_pct = int(round(float(persisted))) if persisted is not None else None

    quality = classify_lead(
        score=score,
        is_authorized=h["is_authorized"],
        has_price=has_price,
        has_qty=has_qty,
        has_contact=False,
        evidence_tier="T7",
    )
    explanation = explain_lead(
        vendor_name=h["vendor_name"],
        is_authorized=h["is_authorized"],
        unit_price=h["unit_price"],
        qty_available=h["qty_available"],
        has_contact=False,
        evidence_tier="T7",
        age_days=age_days,
    )

    return {
        "id": None,
        "requirement_id": None,
        "vendor_name": h["vendor_name"],
        "vendor_email": None,
        "vendor_phone": None,
        "mpn_matched": h["mpn_matched"],
        "manufacturer": h["manufacturer"],
        "qty_available": h["qty_available"],
        "unit_price": h["unit_price"],
        "currency": h["currency"],
        "source_type": h["source_type"],
        "is_authorized": h["is_authorized"],
        "confidence": 0,
        "score": score,
        "source_badge": "Historical",
        "confidence_pct": confidence_pct,
        "confidence_color": confidence_color(confidence_pct) if confidence_pct is not None else None,
        "score_components": h.get("persisted_score_components"),
        "reasoning": None,
        "octopart_url": None,
        "click_url": None,
        "vendor_url": None,
        "vendor_sku": h["vendor_sku"],
        "condition": None,
        "moq": None,
        "date_code": None,
        "packaging": None,
        "lead_time_days": None,
        "lead_time": None,
        "evidence_tier": "T7",
        "created_at": last_seen.isoformat() if last_seen else None,
        "is_historical": False,
        "is_material_history": True,
        "is_stale": age_days > 90,
        "material_last_seen": last_seen.strftime("%b %d") if last_seen else None,
        "material_times_seen": h["times_seen"],
        "material_first_seen": h["first_seen"].strftime("%b %d, %Y") if h["first_seen"] else None,
        "material_card_id": h["material_card_id"],
        "lead_quality": quality,
        "lead_explanation": explanation,
    }


def _audit_card_created(db: Session, card: MaterialCard) -> None:
    """Log a 'created' audit entry for a new material card."""
    try:
        from ..services.audit_service import log_audit

        log_audit(
            db, material_card_id=card.id, action="created", normalized_mpn=card.normalized_mpn, created_by="system"
        )
    except Exception:
        logger.warning("Audit log failed for card {}", getattr(card, "normalized_mpn", "unknown"), exc_info=True)


def resolve_material_card(mpn: str, db: Session, manufacturer: str = "") -> MaterialCard | None:
    """Find or create a MaterialCard for the given MPN.

    Returns the card (flushed, with id set) or None if MPN is too short.

    Uses atomic INSERT ... ON CONFLICT DO NOTHING on PostgreSQL to eliminate
    race conditions when concurrent requests create the same card.  Falls back
    to try/except for SQLite (tests).
    """
    norm = normalize_mpn_key(mpn)
    if not norm:
        return None

    # Fast path — card already exists (no write, cheapest possible check)
    card = db.query(MaterialCard).filter_by(normalized_mpn=norm).filter(MaterialCard.deleted_at.is_(None)).first()
    if card:
        if manufacturer and not card.manufacturer:
            card.manufacturer = manufacturer
        logger.debug("MC_METRIC: action=resolved mpn={} card_id={}", norm, card.id)
        return card

    display = normalize_mpn(mpn) or mpn.strip()

    dialect = db.bind.dialect.name if db.bind else ""
    if dialect == "postgresql":  # pragma: no cover
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(MaterialCard)
            .values(
                normalized_mpn=norm,
                display_mpn=display,
                search_count=0,
                manufacturer=manufacturer,
            )
            .on_conflict_do_nothing(
                index_elements=["normalized_mpn"],
                index_where=MaterialCard.deleted_at.is_(None),
            )
        )
        result = db.execute(stmt)
        db.flush()
        # Re-fetch (unfiltered — may be soft-deleted and needs restoring)
        card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
        if card is None:
            logger.error("MATERIAL_CARD_RESOLVE_FAIL: card missing after ON CONFLICT for mpn={}", norm)
        elif card.deleted_at is not None:
            # Restore soft-deleted card
            card.deleted_at = None
            logger.info("MC_METRIC: action=restored mpn={} card_id={}", norm, card.id)
            _audit_card_created(db, card)
        elif result.rowcount == 0:
            logger.info("MC_METRIC: action=race_resolved mpn={} card_id={}", norm, card.id)
        else:
            logger.info("MC_METRIC: action=created mpn={} card_id={}", norm, card.id)
            _audit_card_created(db, card)
        return card
    else:
        # SQLite / test fallback — use try/except on IntegrityError
        from sqlalchemy.exc import IntegrityError

        try:
            card = MaterialCard(normalized_mpn=norm, display_mpn=display, search_count=0, manufacturer=manufacturer)
            db.add(card)
            db.flush()
            logger.info("MC_METRIC: action=created mpn={} card_id={}", norm, card.id)
            _audit_card_created(db, card)
            return card
        except IntegrityError:
            db.rollback()
            logger.info("MC_METRIC: action=race_resolved mpn={}", norm)
            card = db.query(MaterialCard).filter_by(normalized_mpn=norm).first()
            # Restore if soft-deleted
            if card and card.deleted_at is not None:
                card.deleted_at = None
                db.flush()
                logger.info("MC_METRIC: action=restored mpn={} card_id={}", norm, card.id)
            return card


def _upsert_material_card(pn: str, sightings: list[Sighting], db: Session, now: datetime) -> MaterialCard | None:
    """Upsert material card + link sightings.

    Raises on error — caller handles rollback.
    """
    norm = normalize_mpn_key(pn)
    if not norm:
        return None
    pn_sightings = [s for s in sightings if normalize_mpn_key(s.mpn_matched or "") == norm]
    if not pn_sightings:
        return None

    card = resolve_material_card(pn, db)
    if card is None:
        # resolve_material_card returns None when the card vanished after its
        # ON CONFLICT insert (concurrent hard-delete / race) — previously this
        # crashed with AttributeError; skip the upsert defensively instead.
        # Callers already fall back to resolve_material_card on a None return.
        logger.warning("MATERIAL_CARD_UPSERT_SKIP: no card resolved for mpn={}", norm)
        return None

    card.search_count = (card.search_count or 0) + 1
    card.last_searched_at = now
    if not card.manufacturer:
        for s in pn_sightings:
            if s.manufacturer:
                card.manufacturer = s.manufacturer
                break

    # Batch fetch all existing vendor histories for this card (avoids N+1).
    # Key by normalized vendor name so "ARROW", "Arrow", "arrow" all match.
    existing_vh = {
        normalize_vendor_name(vh.vendor_name): vh
        for vh in db.query(MaterialVendorHistory).filter_by(material_card_id=card.id).all()
    }

    for s in pn_sightings:
        if not s.vendor_name:
            continue
        raw: dict = s.raw_data or {}
        vn_key = normalize_vendor_name(s.vendor_name)
        vh = existing_vh.get(vn_key)

        if vh:
            vh.last_seen = now
            vh.times_seen = (vh.times_seen or 1) + 1
            if s.qty_available is not None:
                vh.last_qty = s.qty_available
            if s.unit_price is not None:
                vh.last_price = s.unit_price
                record_price_snapshot(
                    db=db,
                    material_card_id=card.id,
                    vendor_name=s.vendor_name,
                    price=s.unit_price,
                    currency=s.currency or "USD",
                    quantity=s.qty_available,
                    source="api_sighting",
                )
            if s.currency:
                vh.last_currency = s.currency
            if s.manufacturer:
                vh.last_manufacturer = s.manufacturer
            if s.is_authorized:
                vh.is_authorized = True
            if raw.get("vendor_sku"):
                vh.vendor_sku = raw["vendor_sku"]
        else:
            vn_norm = normalize_vendor_name(s.vendor_name) or s.vendor_name
            new_vh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name=vn_norm,
                vendor_name_normalized=vn_norm,
                source_type=s.source_type,
                is_authorized=s.is_authorized or False,
                first_seen=now,
                last_seen=now,
                times_seen=1,
                last_qty=s.qty_available,
                last_price=s.unit_price,
                last_currency=s.currency or "USD",
                last_manufacturer=s.manufacturer,
                vendor_sku=raw.get("vendor_sku"),
            )
            db.add(new_vh)
            record_price_snapshot(
                db=db,
                material_card_id=card.id,
                vendor_name=s.vendor_name,
                price=s.unit_price,
                currency=s.currency or "USD",
                quantity=s.qty_available,
                source="api_sighting",
            )
            existing_vh[vn_key] = new_vh  # Prevent dupe inserts within batch

    # Link sightings to material card + populate normalized_mpn
    for s in pn_sightings:
        if not s.material_card_id:
            s.material_card_id = card.id
        if not s.normalized_mpn and s.mpn_matched:
            s.normalized_mpn = normalize_mpn_key(s.mpn_matched)

    db.commit()

    # Tag classification: if manufacturer is now set, classify and tag the card
    try:
        if card.manufacturer:
            from ..services.tagging import (
                classify_material_card,
                get_or_create_brand_tag,
                get_or_create_commodity_tag,
                tag_material_card,
            )

            result = classify_material_card(card.normalized_mpn, card.manufacturer, card.category)
            tags_to_apply = []
            if result.get("brand"):
                brand_tag = get_or_create_brand_tag(result["brand"]["name"], db)
                tags_to_apply.append(
                    {
                        "tag_id": brand_tag.id,
                        "source": result["brand"]["source"],
                        "confidence": result["brand"]["confidence"],
                    }
                )
            if result.get("commodity"):
                commodity_tag = get_or_create_commodity_tag(result["commodity"]["name"], db)
                if commodity_tag:
                    tags_to_apply.append(
                        {
                            "tag_id": commodity_tag.id,
                            "source": result["commodity"]["source"],
                            "confidence": result["commodity"]["confidence"],
                        }
                    )
            if tags_to_apply:
                tag_material_card(card.id, tags_to_apply, db)
                db.commit()
    except Exception:
        logger.warning("Tag classification failed for card {}", card.id, exc_info=True)

    return card


def run_deterministic_passes(db: Session, card_ids: list[int] | set[int]) -> None:
    """Run the three inline deterministic enrichment passes over *card_ids*.

    On-create pipeline (on-add enrichment): zero-network, pure CPU + local queries
    (~15ms/card), shared session, no commit — the caller owns the transaction. Order
    mirrors the enrichment worker's second pass (mpn_decode 85 → fru_matrix_decode 84 →
    desc_parse 83) but run order is NOT load-bearing: the F1 tier ladder
    (app/services/spec_tiers.py) arbitrates every write. Idempotent — re-running over
    an existing card re-asserts the same values through the ladder.

    Called by: POST /api/materials/add, the bulk part-number / stock imports
    (routers/materials.py), and search_requirement's write session below — every
    user-action card-creation path. Respects the same feature flags as the worker.
    """
    ids = sorted(int(i) for i in card_ids)
    if not ids:
        return
    from ..config import settings as _settings

    def _run_pass(name: str, fn) -> None:
        # SAVEPOINT per pass: the writers carry per-card savepoints internally, but DB
        # errors escaping those (batched lookup queries, db.get loops, schema-cache
        # loads run outside them) abort the whole PostgreSQL transaction — every later
        # statement then raises InFailedSqlTransaction, so the caller's single commit
        # would 500 and roll back the just-created card(s)/import rows/sightings
        # despite this except "handling" the failure. Rolling back to the savepoint
        # confines a poisoned pass to its own writes. (SQLite tests cannot reproduce
        # the aborted-transaction mode — feedback_sqlite_masks_postgres — so this
        # savepoint IS the guard; verify behavior changes against live PG.)
        try:
            with db.begin_nested():
                logger.info("INLINE_ENRICH: {} {}", name, fn(db, ids))
        except Exception:
            logger.exception(
                "INLINE_ENRICH: {} failed over {} card(s) ids={} — pass rolled back, card creation proceeds",
                name,
                len(ids),
                ids[:50],
            )

    if _settings.mpn_decode_enabled:
        from ..services.mpn_decoder.writer import decode_and_record_specs

        _run_pass("mpn-decode", decode_and_record_specs)
    if _settings.fru_crosswalk_enrich_enabled:
        from ..services.fru_crosswalk_enrich import crosswalk_and_record_specs

        _run_pass("fru-crosswalk", crosswalk_and_record_specs)
    if _settings.desc_parse_enabled:
        from ..services.desc_extractor.writer import extract_and_record_specs

        _run_pass("desc-parse", extract_and_record_specs)


async def _schedule_background_enrichment(card_ids: set[int], db: Session) -> None:
    """Fire background connector enrichment for cards missing a manufacturer."""
    if not card_ids:
        return

    cards_needing_enrichment = (
        db.query(MaterialCard.id, MaterialCard.normalized_mpn)
        .filter(MaterialCard.id.in_(card_ids))
        .filter(MaterialCard.manufacturer.is_(None) | (MaterialCard.manufacturer == ""))
        .all()
    )

    if not cards_needing_enrichment:
        return

    logger.info(f"Scheduling background enrichment for {len(cards_needing_enrichment)} cards")

    async def _enrich_cards():
        from ..database import SessionLocal
        from ..services.enrichment import _apply_enrichment_to_card, enrich_material_card

        session = SessionLocal()
        try:
            for card_id, mpn in cards_needing_enrichment:
                try:
                    result = await enrich_material_card(mpn, session)
                    if result:
                        card = session.get(MaterialCard, card_id)
                        if card:
                            _apply_enrichment_to_card(card, result, session)
                            session.commit()
                except Exception:
                    logger.warning("Background enrichment failed for {}", mpn, exc_info=True)
                    session.rollback()
        finally:
            session.close()

    await safe_background_task(_enrich_cards(), task_name="enrich_search_cards")

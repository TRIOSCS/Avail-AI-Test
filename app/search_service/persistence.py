"""Search package — threaded write orchestrators + the score-and-save core
(_save_sightings) and vendor-email propagation.

W4.5a split of app/search_service.py — pure structural move (see cache.py header).
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MaterialCard, Requirement, Sighting
from ..scoring import score_sighting_v2
from ..services.sourcing_leads import get_vendor_feedback_adjustment, sync_leads_for_sightings
from ..services.vendor_unavailability import apply_to_fresh_sightings
from ..utils.currency import to_usd
from ..utils.normalization import (
    detect_currency,
    normalize_condition,
    normalize_date_code,
    normalize_lead_time,
    normalize_mpn,
    normalize_mpn_key,
    normalize_packaging,
    normalize_price,
    normalize_quantity,
)
from ..utils.normalization_helpers import fix_encoding
from ..vendor_utils import normalize_vendor_name
from . import cache, material_cards, presentation


def _persist_search_write(
    req_id: int,
    fresh: list[dict],
    to_search: list[str],
    succeeded_sources: set[str],
    searched_keys: set[str],
    now: datetime,
    bind,
) -> dict | None:
    """Save fresh sightings + upsert material cards on a worker thread.

    This is the dominant synchronous DB cost of ``search_requirement`` — bulk
    sighting insert, tag propagation, vendor-summary rebuild (all inside
    ``_save_sightings``), the per-MPN material-card upsert loop, and the inline
    deterministic enrichment passes — running directly on the event loop, on
    EVERY search, stalled every other in-flight request for its duration
    (mirrors the vendor-affinity fix at ``_find_affinity_in_thread``, PERF-1).

    Opens its OWN write session bound to *bind* entirely within the thread —
    SQLAlchemy sessions are not thread-safe, so the caller's session never
    crosses the boundary. Returns ONLY plain data (dicts/ids/primitives): no ORM
    object survives the thread, since a detached instance touched from the
    event-loop thread after the session closes would be unsafe.

    Returns ``None`` when the requirement no longer exists (deleted mid-search)
    so the caller can reproduce the original early-return behavior.

    Called by: search_requirement (via asyncio.to_thread)
    Depends on: _save_sightings, _upsert_material_card, resolve_material_card,
                run_deterministic_passes, sighting_to_dict
    """
    from sqlalchemy.orm import sessionmaker

    _WriteSession = sessionmaker(bind=bind, autocommit=False, autoflush=False, expire_on_commit=False)
    write_db = _WriteSession()
    try:
        write_req = write_db.get(Requirement, req_id)
        if not write_req:
            logger.error("Requirement {} not found in write session", req_id)
            return None

        sightings = _save_sightings(fresh, write_req, write_db, succeeded_sources)
        logger.info(f"Req {req_id} ({to_search[0]}): {len(sightings)} fresh sightings")

        # Material card upsert (errors won't break search). Only upsert cards for
        # MPNs we actually searched; cached-side cards are already surfaced via
        # material_card_id linkage in the caller. We also need to ensure the
        # cooldown clock advances even when a search yielded zero sightings —
        # otherwise the next click immediately re-burns the connector quota.
        # `_upsert_material_card` returns None when there were no sightings for
        # that MPN, so we fall back to a card lookup/create to guarantee a card
        # exists, then stamp it.
        #
        # PERF-4: batch-prefetch every to_search MPN's existing card in ONE query
        # instead of resolve_material_card's per-MPN SELECT — avoids N sequential
        # queries for the (common) zero-sighting-for-this-MPN fallback path.
        norm_keys = [k for k in (normalize_mpn_key(pn) for pn in to_search) if k]
        existing_cards = (
            write_db.scalars(
                select(MaterialCard).where(
                    MaterialCard.normalized_mpn.in_(norm_keys), MaterialCard.deleted_at.is_(None)
                )
            ).all()
            if norm_keys
            else []
        )
        card_by_key = {c.normalized_mpn: c for c in existing_cards}

        card_ids: set[int] = set()
        primary_card_id = None
        for pn in to_search:
            try:
                card = material_cards._upsert_material_card(pn, sightings, write_db, now)
                if card is None:
                    norm = normalize_mpn_key(pn)
                    card = card_by_key.get(norm) or material_cards.resolve_material_card(pn, write_db)
                    if card:
                        card_by_key[card.normalized_mpn] = card
                if card:
                    card_ids.add(card.id)
                    # Stamp the cooldown clock on every searched MPN's card.
                    if card.normalized_mpn in searched_keys:
                        card.last_searched_at = now
                    if pn == to_search[0] and not primary_card_id:
                        primary_card_id = card.id
            except Exception as e:
                logger.error("MATERIAL_CARD_UPSERT_FAIL: mpn={} error={}", pn, e)
                write_db.rollback()

        # Link requirement to its primary material card
        if primary_card_id and not write_req.material_card_id:
            write_req.material_card_id = primary_card_id

        # Inline deterministic passes over this search's card ids (same write
        # session, committed with the sightings below) — decoded facets/category
        # are queryable the moment the search returns, without waiting on the
        # worker. NO enrich_requested_at stamp here: search flow rides the
        # existing created_at fast lane + search_count demand ordering.
        material_cards.run_deterministic_passes(write_db, card_ids)

        # Stamp per-requirement search timestamp only when the search actually
        # succeeded. "Success" means at least one connector returned status=ok —
        # i.e. there was a real response from an upstream API (even if it had
        # zero matches). If every connector errored (auth failures, quota
        # exceeded, network), we leave last_searched_at alone so the 5-minute
        # rate guard in routers/sightings.py does not silently suppress the
        # user's next retry.
        if succeeded_sources:
            write_req.last_searched_at = now
        write_db.commit()

        # Convert to plain dicts WHILE the session is still open (sighting_to_dict
        # reads raw_data/created_at/etc off the still-attached ORM instance) — no
        # ORM object crosses back to the event-loop thread.
        return {
            "sighting_dicts": [presentation.sighting_to_dict(s) for s in sightings],
            "card_ids": sorted(card_ids),
            "primary_card_id": primary_card_id,
            "requisition_id": write_req.requisition_id,
        }
    except Exception:
        write_db.rollback()
        raise
    finally:
        write_db.close()


def _persist_interactive_sightings(
    mpn: str,
    raw_hits: list[dict],
    succeeded_sources: set[str],
    now: datetime,
    bind,
) -> dict | None:
    """Persist a live interactive/global search's on-target hits as requirement-less
    Sightings — same write-session pattern as ``_persist_search_write``, minus every
    requirement-scoped step (there is no Requirement row for an interactive search).

    Opens its OWN write session bound to *bind*, entirely within the calling
    thread (mirrors ``_persist_search_write`` — SQLAlchemy sessions are not
    thread-safe). Returns ``None`` when there is nothing to persist.

    Called by: stream_search_mpn (via asyncio.to_thread, AFTER the terminal "done"
               SSE event so persistence never delays the stream). Never called for
               a cache-hit stream.
    Depends on: _save_sightings (req=None path), _upsert_material_card,
                resolve_material_card, run_deterministic_passes
    """
    if not raw_hits:
        return None

    from sqlalchemy.orm import sessionmaker

    _WriteSession = sessionmaker(bind=bind, autocommit=False, autoflush=False, expire_on_commit=False)
    write_db = _WriteSession()
    try:
        sightings = _save_sightings(raw_hits, None, write_db, succeeded_sources)
        logger.info("Interactive search {}: persisted {} requirement-less sighting(s)", mpn, len(sightings))

        card = material_cards._upsert_material_card(mpn, sightings, write_db, now)
        if card is None:
            card = material_cards.resolve_material_card(mpn, write_db)
        card_ids: set[int] = {card.id} if card else set()
        if card:
            card.last_searched_at = now

        material_cards.run_deterministic_passes(write_db, card_ids)
        write_db.commit()

        return {"sighting_count": len(sightings), "card_ids": sorted(card_ids)}
    except Exception:
        write_db.rollback()
        raise
    finally:
        write_db.close()


def _save_sightings(
    fresh: list[dict],
    req: Requirement | None,
    db: Session,
    succeeded_sources: set[str] | None = None,
) -> list[Sighting]:
    """Save fresh connector hits as Sighting rows, scored + deduped.

    ``req`` is optional: when ``None`` the sightings are requirement-less
    (interactive/global "quick search" discoveries persisted by
    ``stream_search_mpn``). In that case every requirement-scoped step — lead
    sync, requirement-level dedup/stamping, vendor-summary rebuild — is skipped
    since those tables' FKs are non-nullable; dedup instead runs against
    existing requirement-less rows by (vendor, mpn). Vendor-card creation,
    material-card upsert (by the caller), scoring, evidence tiers, and tag
    propagation all still run either way.
    """
    from ..models import VendorCard

    requirement_id = req.id if req is not None else None

    # Build vendor-name → (vendor_score, vendor_card_id) lookup (only for
    # vendors in results).
    needed_names = {normalize_vendor_name((r.get("vendor_name") or "").strip()) for r in fresh if r.get("vendor_name")}
    needed_names.discard("")
    if needed_names:
        vendor_cards = (
            db.query(VendorCard.normalized_name, VendorCard.vendor_score, VendorCard.id)
            .filter(VendorCard.normalized_name.in_(needed_names))
            .all()
        )
        vendor_score_map = {vc.normalized_name: vc.vendor_score for vc in vendor_cards}
        vendor_id_map = {vc.normalized_name: vc.id for vc in vendor_cards}
    else:
        vendor_score_map = {}
        vendor_id_map = {}

    # Batch the vendor feedback adjustment: ONE get_vendor_feedback_adjustment
    # call per DISTINCT vendor_card in this save, never per sighting — a save
    # with 200 sightings across 5 vendors issues 5 feedback queries, not 200.
    distinct_vendor_ids = {vc_id for vc_id in vendor_id_map.values() if vc_id}
    feedback_by_vendor_id = {vc_id: get_vendor_feedback_adjustment(db, vc_id) for vc_id in distinct_vendor_ids}

    def _effective_trust_score(norm_name: str) -> float | None:
        """Vendor trust score with the vendor's feedback adjustment applied.

        A do_not_contact vendor is floor-scored (trust <= 15), not dropped — the
        sighting still surfaces, but scores low enough that it never outranks a clean
        vendor's identical listing.
        """
        base = vendor_score_map.get(norm_name)
        if base is None:
            return None
        base_score = float(base)
        adj = feedback_by_vendor_id.get(vendor_id_map.get(norm_name))
        if adj is None:
            return base_score
        adjusted = max(0.0, min(100.0, base_score + adj.confidence_penalty))
        if adj.do_not_contact:
            adjusted = min(adjusted, 15.0)
        return adjusted

    # Connector-aware delete: only remove sightings from sources that returned
    # results.  Sightings from failed/timed-out connectors are preserved.
    # Map nexar → {nexar, octopart} since Octopart results come via NexarConnector
    _SOURCE_ALIASES = {"nexar": {"nexar", "octopart"}}
    expanded: set[str] = set()
    if req is not None and succeeded_sources:
        for s in succeeded_sources:
            expanded.update(_SOURCE_ALIASES.get(s, {s}))

    def _delete_stale_before_insert() -> None:
        """Delete sightings the fresh batch is about to replace.

        Requirement-scoped: connector-aware delete (only sources that
        returned results this run). Requirement-less: dedup existing
        requirement-less rows by (vendor, mpn) — delete the stale row, keep
        the fresh one, mirroring the "keep fresh" merge policy used below for
        requirement-scoped saves. Re-callable (used again on the row-by-row
        retry path below, since a rolled-back commit undoes this delete too).
        """
        if req is not None:
            if succeeded_sources:
                db.query(Sighting).filter(
                    Sighting.requirement_id == requirement_id,
                    Sighting.source_type.in_(expanded),
                ).delete(synchronize_session="fetch")
            else:
                # Fallback: no source info → wipe all (legacy behaviour)
                db.query(Sighting).filter_by(requirement_id=requirement_id).delete()
            return

        incoming_keys = {
            (normalize_vendor_name((r.get("vendor_name") or "").strip()), normalize_mpn_key(r.get("mpn_matched") or ""))
            for r in fresh
        }
        incoming_keys = {k for k in incoming_keys if k[0] and k[1]}
        if not incoming_keys:
            return
        vendor_norms = {k[0] for k in incoming_keys}
        mpn_keys = {k[1] for k in incoming_keys}
        stale_candidates = (
            db.query(Sighting)
            .filter(
                Sighting.requirement_id.is_(None),
                Sighting.vendor_name_normalized.in_(vendor_norms),
                Sighting.normalized_mpn.in_(mpn_keys),
            )
            .all()
        )
        stale_ids = [c.id for c in stale_candidates if (c.vendor_name_normalized, c.normalized_mpn) in incoming_keys]
        if stale_ids:
            db.query(Sighting).filter(Sighting.id.in_(stale_ids)).delete(synchronize_session="fetch")

    _delete_stale_before_insert()
    db.flush()

    sightings = []
    for r in fresh:
        # Normalize mpn_matched (uppercase, strip) and vendor_name (trim, fix encoding)
        raw_mpn = r.get("mpn_matched")
        clean_mpn = normalize_mpn(raw_mpn) or raw_mpn
        raw_vendor = r.get("vendor_name", "Unknown")
        clean_vendor = fix_encoding((raw_vendor or "").strip()) or raw_vendor

        # Normalize numeric and enum fields from raw connector data
        raw_qty = r.get("qty_available")
        clean_qty = normalize_quantity(raw_qty)
        if clean_qty is None and isinstance(raw_qty, (int, float)) and raw_qty > 0:
            clean_qty = int(raw_qty)

        raw_price = r.get("unit_price")
        clean_price = normalize_price(raw_price)
        if clean_price is None and isinstance(raw_price, (int, float)) and raw_price > 0:
            clean_price = float(raw_price)

        raw_currency = r.get("currency") or "USD"
        clean_currency = detect_currency(raw_currency) if raw_currency else "USD"

        clean_condition = normalize_condition(r.get("condition"))
        clean_packaging = normalize_packaging(r.get("packaging"))
        clean_date_code = normalize_date_code(r.get("date_code"))
        clean_lead_time_days = normalize_lead_time(r.get("lead_time"))
        raw_moq = r.get("moq")

        # Normalize confidence to 0-1 range (connectors use 1-5 integer scale)
        raw_conf = r.get("confidence", 0) or 0
        norm_conf = raw_conf / 5.0 if raw_conf > 1 else raw_conf

        from ..evidence_tiers import tier_for_sighting

        is_auth = r.get("is_authorized", False)
        s = Sighting(
            requirement_id=requirement_id,
            vendor_name=clean_vendor,
            vendor_name_normalized=normalize_vendor_name(clean_vendor),
            vendor_email=r.get("vendor_email"),
            vendor_phone=r.get("vendor_phone"),
            mpn_matched=clean_mpn,
            # Set the dedup key at insert — the material-card upsert's backfill
            # (which only fills when missing) can be skipped on failure, and the
            # requirement-less stale-row dedup above filters on this column.
            normalized_mpn=normalize_mpn_key(clean_mpn) if clean_mpn else None,
            material_card_id=r.get("material_card_id"),
            manufacturer=r.get("manufacturer"),
            qty_available=clean_qty,
            unit_price=clean_price,
            currency=clean_currency,
            moq=raw_moq if raw_moq and raw_moq > 0 else None,
            source_type=r.get("source_type"),
            is_authorized=is_auth,
            confidence=norm_conf,
            condition=clean_condition,
            packaging=clean_packaging,
            date_code=clean_date_code,
            lead_time_days=clean_lead_time_days,
            lead_time=r.get("lead_time"),
            # Strip the internal freshness tag (_source_age_hours) — raw_data is
            # meant to mirror exactly what the connector returned.
            raw_data={k: v for k, v in r.items() if k != "_source_age_hours"},
            evidence_tier=tier_for_sighting(r.get("source_type"), is_auth),
            # Spec-code resolver lineage (spec §6). Both null on the normal
            # path; populated by the search_requirement re-fanout block when
            # the sighting was discovered via an AVL MPN resolved from an
            # OEM spec code.
            resolved_via_spec_code=r.get("resolved_via_spec_code"),
            source_mpn=r.get("source_mpn"),
            created_at=datetime.now(UTC),
        )
        db.add(s)
        sightings.append(s)

    # Compute the ONE multi-factor v2 score with median price context (spec §9: this
    # is the only place a sighting score is produced; display reads it back). Prices
    # are converted to USD before the median and the per-offer comparison (currency-blind
    # price scoring bug — a search mixing e.g. JPY and USD listings previously compared
    # raw numbers across currencies). `sightings` is built 1:1 from `fresh` above (one
    # Sighting appended per row, no filtering), so zip pairs each new Sighting with its
    # source dict for the real freshness tag: age_hours=0.0 is correct for a genuinely
    # live connector hit, but a row served from the 15-min search-result Redis cache
    # (`_fetch_fresh`'s cache-HIT path) carries its real elapsed age in
    # `_source_age_hours` instead of falsely scoring as brand-fresh.
    prices_usd = [
        p
        for p in (to_usd(s.unit_price, s.currency) for s in sightings if s.unit_price and s.unit_price > 0)
        if p is not None
    ]
    median_price = cache._median(prices_usd)
    target_qty = (req.target_qty if req.target_qty else None) if req is not None else None
    for s, r in zip(sightings, fresh):
        norm_name = s.vendor_name_normalized or ""
        v2_total, v2_comp = score_sighting_v2(
            vendor_score=_effective_trust_score(norm_name),
            is_authorized=s.is_authorized,
            unit_price=to_usd(s.unit_price, s.currency),
            median_price=median_price,
            qty_available=s.qty_available,
            target_qty=target_qty,
            age_hours=r.get("_source_age_hours", 0.0),
            has_price=s.unit_price is not None,
            has_qty=s.qty_available is not None,
            has_lead_time=s.lead_time_days is not None,
            has_condition=s.condition is not None,
        )
        s.score = v2_total
        s.score_components = v2_comp

    # Re-apply durable vendor+part unavailability knowledge before the rows
    # commit — a re-search (delete + recreate) must never resurrect a dead
    # vendor. Policy overrides O1/O2 are evaluated per row inside the service.
    # Requirement-scoped only: the suppression matrix logs against a real
    # requirement (see vendor_unavailability._release_record) and requires
    # requirement.primary_mpn for its candidate-key fallback.
    if req is not None:
        apply_to_fresh_sightings(db, req, sightings)

    try:
        db.commit()
    except Exception as e:  # pragma: no cover
        # One bad row shouldn't kill the entire batch — rollback and retry
        # one-by-one, skipping any rows that violate constraints.
        logger.warning(f"Bulk sighting commit failed ({e}), retrying row-by-row")
        db.rollback()
        # Re-delete old/stale sightings — rollback undid the delete above.
        _delete_stale_before_insert()
        db.flush()
        saved = []
        for s in sightings:
            try:
                db.merge(s)
                db.flush()
                saved.append(s)
            except Exception:
                db.rollback()
                logger.warning(f"Skipping bad sighting: {s.source_type}/{s.vendor_name}/{s.mpn_matched}")
        db.commit()
        sightings = saved

    # Dedup: if a vendor+MPN exists in both old (preserved) and fresh, keep fresh.
    # Requirement-scoped only — the requirement-less path already deleted its
    # stale (vendor, mpn) matches up front in _delete_stale_before_insert.
    if req is not None and succeeded_sources and expanded:
        fresh_keys = {(s.vendor_name.lower(), (s.mpn_matched or "").lower()) for s in sightings}
        old = (
            db.query(Sighting)
            .filter(
                Sighting.requirement_id == requirement_id,
                ~Sighting.source_type.in_(expanded),
            )
            .all()
        )
        for o in old:
            if (o.vendor_name.lower(), (o.mpn_matched or "").lower()) in fresh_keys:
                db.delete(o)
        db.commit()

    # Propagate vendor emails from search results to VendorContact records
    _propagate_vendor_emails(sightings, db)

    # Write-through canonical sourcing leads + evidence without changing read
    # paths. Requirement-scoped only — SourcingLead.requirement_id is NOT NULL.
    if req is not None:
        try:
            sync_leads_for_sightings(db, req, sightings)
        except Exception:
            logger.warning("Sourcing lead write-through failed for requirement {}", req.id, exc_info=True)

    # Tag propagation: propagate material card tags to vendor entities
    try:
        from ..models import VendorCard
        from ..services.tagging import propagate_tags_to_entity

        # PERF-5: resolve every sighting's VendorCard in ONE IN(normalized_names)
        # query instead of one .first() per sighting. normalized_name is unique on
        # VendorCard, so the dict lookup returns exactly the row .first() would have
        # (or None). The (material_card_id, vn_norm) list preserves the original
        # sighting order, so propagate_tags_to_entity is called identically.
        tag_targets: list[tuple[int, str]] = []
        for s in sightings:
            if not s.material_card_id or not s.vendor_name:
                continue
            vn_norm = normalize_vendor_name(s.vendor_name)
            if not vn_norm:
                continue
            tag_targets.append((s.material_card_id, vn_norm))
        if tag_targets:
            norms = {vn for _, vn in tag_targets}
            card_by_norm = {
                vc.normalized_name: vc
                for vc in db.query(VendorCard).filter(VendorCard.normalized_name.in_(norms)).all()
            }
            for material_card_id, vn_norm in tag_targets:
                vc = card_by_norm.get(vn_norm)
                if vc:
                    propagate_tags_to_entity("vendor_card", vc.id, material_card_id, 1.0, db)
        db.commit()
    except Exception:
        logger.warning("Tag propagation failed for sightings", exc_info=True)

    # Rebuild vendor-level sighting summaries for aggregated display.
    # Requirement-scoped only — VendorSightingSummary.requirement_id is NOT NULL.
    if req is not None:
        from ..services.sighting_aggregation import rebuild_vendor_summaries_from_sightings

        rebuild_vendor_summaries_from_sightings(db, requirement_id, sightings)

    return sightings  # type: ignore[return-value]  # mypy misinfers element type via ORM columns


def _propagate_vendor_emails(sightings: list[Sighting], db: Session):
    """Create VendorContact records from sighting emails (e.g. BrokerBin)."""
    from ..models import VendorCard, VendorContact
    from ..vendor_utils import merge_emails_into_card, normalize_vendor_name

    # Collect unique vendor_name -> email pairs
    email_map: dict[str, set[str]] = {}
    phone_map: dict[str, set[str]] = {}
    for s in sightings:
        if not s.vendor_email or "@" not in s.vendor_email:
            continue
        vn = (s.vendor_name or "").strip()
        if not vn:
            continue
        email_map.setdefault(vn, set()).add(s.vendor_email.strip().lower())
        if s.vendor_phone:
            phone_map.setdefault(vn, set()).add(s.vendor_phone.strip())

    if not email_map:
        return

    # PERF-5: resolve every vendor's VendorCard in ONE IN(normalized_names) query
    # instead of one .first() per vendor. normalized_name is unique on VendorCard,
    # so the dict lookup returns exactly the row .first() would have (or None).
    # Vendor names that normalize to the same key share the one card object, just as
    # repeated .first() calls returned the same identity-mapped instance.
    name_norms = {name: normalize_vendor_name(name) for name in email_map}
    wanted_norms = {n for n in name_norms.values() if n}
    card_by_norm = (
        {
            card.normalized_name: card
            for card in db.query(VendorCard).filter(VendorCard.normalized_name.in_(wanted_norms)).all()
        }
        if wanted_norms
        else {}
    )

    for vendor_name, emails in email_map.items():
        norm = name_norms.get(vendor_name)
        if not norm:
            continue

        card = card_by_norm.get(norm)
        if not card:
            continue

        # Merge emails into VendorCard.emails JSON array
        merge_emails_into_card(card, list(emails))

        # Create VendorContact records if not exists
        for email in emails:
            existing = db.query(VendorContact).filter_by(vendor_card_id=card.id, email=email).first()
            if existing:
                existing.last_seen_at = datetime.now(UTC)
                continue

            contact = VendorContact(
                vendor_card_id=card.id,
                email=email,
                source="brokerbin",
                confidence=60,
                contact_type="company",
            )
            db.add(contact)

        # Also add phones if available
        phones = phone_map.get(vendor_name, set())
        if phones:
            from ..vendor_utils import merge_phones_into_card

            merge_phones_into_card(card, list(phones))

    try:
        db.commit()
    except Exception as e:
        logger.warning("Failed to propagate vendor emails: {}", e)
        db.rollback()

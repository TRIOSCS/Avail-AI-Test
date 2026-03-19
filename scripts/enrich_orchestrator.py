"""Autonomous enrichment orchestrator — runs all phases end-to-end inside Docker.

Submits Anthropic Batch API requests, polls until complete, applies results,
then moves to the next phase. Tracks state in DB so it can resume after
container restarts.

Usage (inside Docker):
    python scripts/enrich_orchestrator.py              # Full pipeline, dry-run
    python scripts/enrich_orchestrator.py --apply      # Full pipeline, writes to DB
    python scripts/enrich_orchestrator.py --resume     # Resume from last checkpoint

Called by: manual invocation inside Docker container
Depends on: app.database, app.models, app.utils.claude_client, app.services
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

from loguru import logger

sys.path.insert(0, os.environ.get("APP_ROOT", "/app"))

from sqlalchemy import func

from app.database import SessionLocal
from app.models.enrichment_run import EnrichmentRun
from app.models.intelligence import MaterialCard
from app.models.sourcing import Sighting
from scripts.enrich_batch import (
    BATCH_SIZE,
    _build_batch_requests,
)
from scripts.enrich_batch import (
    VALID_CATEGORIES as BATCH_CATEGORIES,
)

# Import phase logic from individual scripts
from scripts.enrich_from_sightings import SOURCE_PRIORITY, enrich_card_from_sightings
from scripts.enrich_specs_batch import (
    COMMODITY_SPECS,
    _build_spec_prompt,
    _build_spec_schema,
    _specs_to_summary,
)

POLL_INTERVAL = 60  # seconds between batch status checks
MAX_POLL_TIME = 86400  # 24 hours max wait per batch
CATEGORY_CONFIDENCE_MIN = 0.90  # minimum confidence to accept AI-assigned category
DESCRIPTION_CONFIDENCE_MIN = 0.90  # minimum confidence to accept AI-generated description
SPEC_CONFIDENCE_MIN = 0.85  # minimum confidence for spec extraction


# ── Helpers ──────────────────────────────────────────────────────────


def _get_or_create_run(db, phase: str, run_id: str = None) -> EnrichmentRun:
    """Get existing run or create new one."""
    if run_id:
        run = db.query(EnrichmentRun).filter(EnrichmentRun.run_id == run_id).first()
        if run:
            return run

    run = EnrichmentRun(
        run_id=run_id or f"{phase}_{uuid.uuid4().hex[:8]}",
        phase=phase,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    return run


def _complete_run(db, run: EnrichmentRun, stats: dict):
    run.status = "completed"
    run.stats = stats
    run.completed_at = datetime.now(timezone.utc)
    db.commit()


def _fail_run(db, run: EnrichmentRun, error: str):
    run.status = "failed"
    run.error_message = error
    db.commit()


# ── Phase 0: Reclassify coarse categories ────────────────────────────


async def phase_0_reclassify(db, dry_run: bool) -> dict:
    """Reclassify legacy coarse categories to granular 45-category taxonomy."""
    logger.info("═══ Phase 0: Reclassify coarse categories ═══")

    COARSE_TO_GRANULAR = {
        "processors": "cpu",
        "memory": "dram",
        "storage": "ssd",
        "servers": "server_chassis",
        "Microprocessors": "microprocessors",
    }

    run = _get_or_create_run(db, "phase_0_reclassify")
    stats = {"total_reclassified": 0}

    for old_cat, new_cat in COARSE_TO_GRANULAR.items():
        if dry_run:
            count = (
                db.query(func.count(MaterialCard.id))
                .filter(MaterialCard.deleted_at.is_(None), MaterialCard.category == old_cat)
                .scalar()
            )
        else:
            count = (
                db.query(MaterialCard)
                .filter(MaterialCard.deleted_at.is_(None), MaterialCard.category == old_cat)
                .update({"category": new_cat}, synchronize_session=False)
            )
            db.commit()
        if count:
            logger.info(f"  {old_cat} → {new_cat}: {count} cards")
            stats["total_reclassified"] += count

    # Fix junk categories
    if dry_run:
        junk = (
            db.query(func.count(MaterialCard.id))
            .filter(MaterialCard.deleted_at.is_(None), func.length(MaterialCard.category) > 25)
            .scalar()
        )
    else:
        junk = (
            db.query(MaterialCard)
            .filter(MaterialCard.deleted_at.is_(None), func.length(MaterialCard.category) > 25)
            .update({"category": "other"}, synchronize_session=False)
        )
        db.commit()
    if junk:
        logger.info(f"  junk (len>25) → other: {junk} cards")
        stats["total_reclassified"] += junk

    _complete_run(db, run, stats)
    logger.info(f"Phase 0 complete: {stats}")
    return stats


# ── Phase 1: Mine existing sighting data ─────────────────────────────


async def phase_1_mine_sightings(db, dry_run: bool) -> dict:
    """Extract descriptions, manufacturers, datasheet URLs from sighting raw_data."""
    logger.info("═══ Phase 1: Mine sighting data ═══")

    run = _get_or_create_run(db, "phase_1_sightings")
    stats = {"processed": 0, "updated": 0, "skipped": 0, "desc_updated": 0, "mfg_updated": 0, "ds_updated": 0}

    chunk_size = 500
    offset = 0

    while True:
        card_ids_q = (
            db.query(func.distinct(Sighting.material_card_id))
            .filter(Sighting.material_card_id.isnot(None))
            .order_by(Sighting.material_card_id)
            .offset(offset)
            .limit(chunk_size)
            .all()
        )
        card_ids = [r[0] for r in card_ids_q]
        if not card_ids:
            break

        cards = db.query(MaterialCard).filter(MaterialCard.id.in_(card_ids), MaterialCard.deleted_at.is_(None)).all()
        card_map = {c.id: c for c in cards}

        sightings = (
            db.query(
                Sighting.material_card_id,
                Sighting.source_type,
                Sighting.manufacturer,
                Sighting.is_authorized,
                Sighting.raw_data,
            )
            .filter(Sighting.material_card_id.in_(card_ids))
            .all()
        )

        card_sightings: dict[int, list] = {}
        for s in sightings:
            cid = s.material_card_id
            if cid not in card_sightings:
                card_sightings[cid] = []
            card_sightings[cid].append((s.source_type, s.manufacturer, s.is_authorized, s.raw_data))

        for cid in card_sightings:
            card_sightings[cid].sort(key=lambda x: SOURCE_PRIORITY.get(x[0] or "", 0), reverse=True)

        for cid, sight_list in card_sightings.items():
            card = card_map.get(cid)
            if not card:
                continue

            updates = enrich_card_from_sightings(card, sight_list, dry_run=dry_run)
            stats["processed"] += 1

            if updates:
                stats["updated"] += 1
                if "description" in updates:
                    stats["desc_updated"] += 1
                if "manufacturer" in updates:
                    stats["mfg_updated"] += 1
                if "datasheet_url" in updates:
                    stats["ds_updated"] += 1
            else:
                stats["skipped"] += 1

        if not dry_run:
            db.commit()

        offset += chunk_size
        if stats["processed"] % 5000 == 0:
            logger.info(f"  Phase 1 progress: {stats}")
            run.progress = stats
            db.commit()

    _complete_run(db, run, stats)
    logger.info(f"Phase 1 complete: {stats}")
    return stats


# ── Phase 2: AI category + description + package (Sonnet Batch) ──────


async def phase_2_batch_enrichment(db, dry_run: bool) -> dict:
    """Submit all cards to Sonnet Batch API for category + description + package."""
    from app.utils.claude_client import claude_batch_submit

    logger.info("═══ Phase 2: AI batch enrichment (Sonnet) ═══")

    # Check for resumable run
    existing = (
        db.query(EnrichmentRun)
        .filter(EnrichmentRun.phase == "phase_2_batch", EnrichmentRun.status.in_(["running", "submitted"]))
        .first()
    )

    if existing and existing.batch_ids:
        logger.info(f"Resuming Phase 2 run {existing.run_id} — polling batch results")
        return await _poll_and_apply_phase2(db, existing, dry_run)

    run = _get_or_create_run(db, "phase_2_batch")

    # Query all cards
    rows = (
        db.query(
            MaterialCard.id,
            MaterialCard.display_mpn,
            MaterialCard.manufacturer,
            MaterialCard.description,
        )
        .filter(MaterialCard.deleted_at.is_(None))
        .order_by(MaterialCard.search_count.desc().nullslast())
        .all()
    )
    logger.info(f"  Total cards to enrich: {len(rows)}")

    all_cards = [
        {"id": r.id, "display_mpn": r.display_mpn, "manufacturer": r.manufacturer, "description": r.description}
        for r in rows
    ]

    # Build batch requests
    requests = _build_batch_requests(all_cards)
    logger.info(f"  Built {len(requests)} batch requests ({len(all_cards)} cards)")

    # Store request map for result application
    request_map = {}
    idx = 0
    for req in requests:
        chunk = all_cards[idx : idx + BATCH_SIZE]
        request_map[req["custom_id"]] = [{"id": c["id"], "mpn": c["display_mpn"]} for c in chunk]
        idx += BATCH_SIZE

    if dry_run:
        stats = {"total_cards": len(all_cards), "total_requests": len(requests), "mode": "dry_run"}
        _complete_run(db, run, stats)
        logger.info(f"Phase 2 DRY RUN: {stats}")
        return stats

    # Submit batches (Anthropic limit: 10K requests per batch)
    batch_ids = []
    for chunk_start in range(0, len(requests), 10000):
        chunk = requests[chunk_start : chunk_start + 10000]
        batch_id = await claude_batch_submit(chunk)
        if batch_id:
            batch_ids.append(batch_id)
            logger.info(f"  Submitted batch: {batch_id} ({len(chunk)} requests)")
        else:
            logger.error(f"  Failed to submit batch chunk at {chunk_start}")

    if not batch_ids:
        _fail_run(db, run, "All batch submissions failed")
        return {"error": "All submissions failed"}

    # Save state for resume
    run.batch_ids = batch_ids
    run.request_map = request_map
    run.status = "submitted"
    run.progress = {"submitted": len(all_cards), "batch_count": len(batch_ids)}
    db.commit()

    logger.info(f"  Submitted {len(batch_ids)} batches — polling for results")
    return await _poll_and_apply_phase2(db, run, dry_run)


async def _poll_and_apply_phase2(db, run: EnrichmentRun, dry_run: bool) -> dict:
    """Poll batch results and apply to MaterialCards."""
    from app.utils.claude_client import claude_batch_results

    stats = {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}
    pending_batches = list(run.batch_ids)
    elapsed = 0

    while pending_batches and elapsed < MAX_POLL_TIME:
        still_pending = []
        for batch_id in pending_batches:
            results = await claude_batch_results(batch_id)
            if results is None:
                still_pending.append(batch_id)
                continue

            # Apply results
            for custom_id, result_data in results.items():
                if result_data is None:
                    stats["errors"] += 1
                    continue

                card_meta_list = run.request_map.get(custom_id, [])
                parts = result_data.get("parts", [])

                for card_info, ai_part in zip(card_meta_list, parts):
                    stats["processed"] += 1
                    cat = ai_part.get("category", "other")
                    cat_conf = ai_part.get("category_confidence", 0.0)
                    desc = ai_part.get("description")
                    desc_conf = ai_part.get("description_confidence", 0.0)
                    mfg = ai_part.get("manufacturer")
                    pkg = ai_part.get("package_type")

                    if cat not in BATCH_CATEGORIES:
                        cat = "other"

                    updates = {}
                    if cat_conf >= CATEGORY_CONFIDENCE_MIN and cat != "other":
                        updates["category"] = cat
                    if desc and desc_conf >= DESCRIPTION_CONFIDENCE_MIN:
                        updates["description"] = desc[:1000]
                    if mfg and isinstance(mfg, str) and mfg.strip():
                        updates["manufacturer"] = mfg.strip()[:255]
                    if pkg and isinstance(pkg, str) and pkg.strip():
                        updates["package_type"] = pkg.strip()[:100]

                    if not updates:
                        stats["skipped"] += 1
                        continue

                    if not dry_run:
                        db.query(MaterialCard).filter(MaterialCard.id == card_info["id"]).update(
                            {
                                **updates,
                                "enrichment_source": "sonnet_batch_v2",
                                "enriched_at": datetime.now(timezone.utc),
                            },
                            synchronize_session=False,
                        )
                    stats["updated"] += 1

                if not dry_run:
                    db.commit()

            logger.info(f"  Batch {batch_id} processed: {stats}")

        pending_batches = still_pending
        if pending_batches:
            run.progress = {**stats, "pending_batches": len(pending_batches)}
            db.commit()
            logger.info(f"  {len(pending_batches)} batches still processing — waiting {POLL_INTERVAL}s")
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

    _complete_run(db, run, stats)
    logger.info(f"Phase 2 complete: {stats}")
    return stats


# ── Phase 3: Structured spec extraction (Sonnet Batch) ───────────────


async def phase_3_spec_extraction(db, dry_run: bool) -> dict:
    """Extract structured specs per commodity via Sonnet Batch API."""
    from app.utils.claude_client import claude_batch_submit

    logger.info("═══ Phase 3: Structured spec extraction ═══")

    all_stats = {}

    for category in COMMODITY_SPECS:
        # Check for resumable run
        existing = (
            db.query(EnrichmentRun)
            .filter(
                EnrichmentRun.phase == f"phase_3_specs_{category}",
                EnrichmentRun.status.in_(["running", "submitted"]),
            )
            .first()
        )

        if existing and existing.batch_ids:
            logger.info(f"  Resuming {category} spec extraction")
            stats = await _poll_and_apply_specs(db, existing, category, dry_run)
            all_stats[category] = stats
            continue

        # Query cards with this category
        rows = (
            db.query(MaterialCard.id, MaterialCard.display_mpn, MaterialCard.manufacturer, MaterialCard.description)
            .filter(
                MaterialCard.deleted_at.is_(None),
                MaterialCard.category == category,
                MaterialCard.description.isnot(None),
                MaterialCard.description != "",
            )
            .order_by(MaterialCard.search_count.desc().nullslast())
            .all()
        )

        if not rows:
            logger.info(f"  [{category}] No cards — skipping")
            continue

        logger.info(f"  [{category}] {len(rows)} cards")
        all_cards = [
            {"id": r.id, "display_mpn": r.display_mpn, "manufacturer": r.manufacturer, "description": r.description}
            for r in rows
        ]

        run = _get_or_create_run(db, f"phase_3_specs_{category}")

        if dry_run:
            run.stats = {"total_cards": len(all_cards), "mode": "dry_run"}
            _complete_run(db, run, run.stats)
            all_stats[category] = run.stats
            continue

        # Build and submit batch
        system = (
            "You are an expert electronic component engineer. Extract structured specifications "
            "from part numbers and descriptions. Only include specs you are confident about."
        )
        schema = _build_spec_schema(category)
        requests = []
        request_map = {}

        for i in range(0, len(all_cards), BATCH_SIZE):
            chunk = all_cards[i : i + BATCH_SIZE]
            custom_id = f"specs_{category}_{i}"
            prompt = _build_spec_prompt(category, chunk)
            requests.append(
                {
                    "custom_id": custom_id,
                    "prompt": prompt,
                    "schema": schema,
                    "system": system,
                    "model_tier": "smart",
                    "max_tokens": 8192,
                }
            )
            request_map[custom_id] = [{"id": c["id"], "mpn": c["display_mpn"]} for c in chunk]

        batch_id = await claude_batch_submit(requests)
        if not batch_id:
            _fail_run(db, run, "Batch submission failed")
            all_stats[category] = {"error": "submission_failed"}
            continue

        run.batch_ids = [batch_id]
        run.request_map = request_map
        run.status = "submitted"
        db.commit()

        stats = await _poll_and_apply_specs(db, run, category, dry_run)
        all_stats[category] = stats

    logger.info(f"Phase 3 complete: {json.dumps(all_stats, indent=2)}")
    return all_stats


async def _poll_and_apply_specs(db, run: EnrichmentRun, category: str, dry_run: bool) -> dict:
    """Poll and apply spec extraction results."""
    from app.utils.claude_client import claude_batch_results

    stats = {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}
    elapsed = 0

    while elapsed < MAX_POLL_TIME:
        all_done = True
        for batch_id in run.batch_ids:
            results = await claude_batch_results(batch_id)
            if results is None:
                all_done = False
                continue

            for custom_id, result_data in results.items():
                if result_data is None:
                    stats["errors"] += 1
                    continue

                card_meta_list = run.request_map.get(custom_id, [])
                parts = result_data.get("parts", [])

                for card_info, ai_part in zip(card_meta_list, parts):
                    stats["processed"] += 1
                    summary = _specs_to_summary(category, ai_part)
                    if not summary:
                        stats["skipped"] += 1
                        continue

                    if not dry_run:
                        db.query(MaterialCard).filter(MaterialCard.id == card_info["id"]).update(
                            {"specs_summary": summary},
                            synchronize_session=False,
                        )
                    stats["updated"] += 1

                if not dry_run:
                    db.commit()

        if all_done:
            break

        run.progress = stats
        db.commit()
        logger.info(f"  [{category}] Waiting for batch results — {stats}")
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    _complete_run(db, run, stats)
    logger.info(f"  [{category}] Specs complete: {stats}")
    return stats


# ── Phase 4: Premium descriptions (Sonnet Batch) ─────────────────────


async def phase_4_premium_descriptions(db, dry_run: bool) -> dict:
    """Generate rich technical descriptions for all cards with a category."""
    from app.utils.claude_client import claude_batch_submit

    logger.info("═══ Phase 4: Premium descriptions (Sonnet) ═══")

    existing = (
        db.query(EnrichmentRun)
        .filter(EnrichmentRun.phase == "phase_4_descriptions", EnrichmentRun.status.in_(["running", "submitted"]))
        .first()
    )

    if existing and existing.batch_ids:
        logger.info("Resuming Phase 4")
        return await _poll_and_apply_descriptions(db, existing, dry_run)

    run = _get_or_create_run(db, "phase_4_descriptions")

    rows = (
        db.query(
            MaterialCard.id,
            MaterialCard.display_mpn,
            MaterialCard.manufacturer,
            MaterialCard.category,
            MaterialCard.description,
            MaterialCard.specs_summary,
        )
        .filter(
            MaterialCard.deleted_at.is_(None),
            MaterialCard.category.isnot(None),
            MaterialCard.category != "other",
        )
        .order_by(MaterialCard.search_count.desc().nullslast())
        .all()
    )
    logger.info(f"  Cards for premium descriptions: {len(rows)}")

    if not rows:
        _complete_run(db, run, {"total": 0})
        return {"total": 0}

    system = (
        "You are an expert electronic component engineer writing professional product "
        "descriptions for a component sourcing platform. Write accurate, technical "
        "descriptions based on the MPN, manufacturer, category, and any specs provided.\n\n"
        "Rules:\n"
        "- description: 2-3 sentences. Include key specs, applications, and compatibility.\n"
        "- specs_summary: Concise key specs in format 'Spec: Value | Spec: Value'\n"
        "- Do NOT hallucinate specs. Only include what you can confidently determine.\n"
        "- confidence: your confidence (0.0-1.0) that the description is accurate."
    )

    schema = {
        "type": "object",
        "properties": {
            "parts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "mpn": {"type": "string"},
                        "description": {"type": ["string", "null"]},
                        "specs_summary": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                    },
                    "required": ["mpn", "description", "confidence"],
                },
            }
        },
        "required": ["parts"],
    }

    requests = []
    request_map = {}

    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i : i + BATCH_SIZE]
        custom_id = f"desc_{i}"

        lines = []
        card_meta = []
        for r in chunk:
            entry = f"- MPN: {r.display_mpn}"
            if r.manufacturer:
                entry += f" | Mfg: {r.manufacturer}"
            if r.category:
                entry += f" | Category: {r.category}"
            if r.specs_summary:
                entry += f" | Specs: {r.specs_summary[:200]}"
            elif r.description and len(r.description) < 80:
                entry += f" | Context: {r.description}"
            lines.append(entry)
            card_meta.append({"id": r.id, "mpn": r.display_mpn})

        prompt = (
            "Write professional technical descriptions for these electronic components:\n\n"
            + "\n".join(lines)
            + "\n\nReturn a JSON object with a 'parts' array, one entry per MPN above, in order."
        )

        requests.append(
            {
                "custom_id": custom_id,
                "prompt": prompt,
                "schema": schema,
                "system": system,
                "model_tier": "smart",
                "max_tokens": 8192,
            }
        )
        request_map[custom_id] = card_meta

    if dry_run:
        stats = {"total_cards": len(rows), "total_requests": len(requests), "mode": "dry_run"}
        _complete_run(db, run, stats)
        logger.info(f"Phase 4 DRY RUN: {stats}")
        return stats

    batch_ids = []
    for chunk_start in range(0, len(requests), 10000):
        chunk = requests[chunk_start : chunk_start + 10000]
        batch_id = await claude_batch_submit(chunk)
        if batch_id:
            batch_ids.append(batch_id)
            logger.info(f"  Submitted description batch: {batch_id}")

    if not batch_ids:
        _fail_run(db, run, "All batch submissions failed")
        return {"error": "submissions failed"}

    run.batch_ids = batch_ids
    run.request_map = request_map
    run.status = "submitted"
    db.commit()

    return await _poll_and_apply_descriptions(db, run, dry_run)


async def _poll_and_apply_descriptions(db, run: EnrichmentRun, dry_run: bool) -> dict:
    """Poll and apply premium description results."""
    from app.utils.claude_client import claude_batch_results

    stats = {"processed": 0, "updated": 0, "skipped": 0, "errors": 0}
    pending = list(run.batch_ids)
    elapsed = 0

    while pending and elapsed < MAX_POLL_TIME:
        still_pending = []
        for batch_id in pending:
            results = await claude_batch_results(batch_id)
            if results is None:
                still_pending.append(batch_id)
                continue

            for custom_id, result_data in results.items():
                if result_data is None:
                    stats["errors"] += 1
                    continue

                card_meta = run.request_map.get(custom_id, [])
                parts = result_data.get("parts", [])

                for card_info, ai_part in zip(card_meta, parts):
                    stats["processed"] += 1
                    desc = ai_part.get("description")
                    specs = ai_part.get("specs_summary")
                    conf = ai_part.get("confidence", 0.0)

                    if not desc or conf < DESCRIPTION_CONFIDENCE_MIN:
                        stats["skipped"] += 1
                        continue

                    updates = {
                        "description": desc[:1000],
                        "enrichment_source": "sonnet_premium_v4",
                        "enriched_at": datetime.now(timezone.utc),
                    }
                    if specs:
                        updates["specs_summary"] = specs

                    if not dry_run:
                        db.query(MaterialCard).filter(MaterialCard.id == card_info["id"]).update(
                            updates,
                            synchronize_session=False,
                        )
                    stats["updated"] += 1

                if not dry_run:
                    db.commit()

        pending = still_pending
        if pending:
            run.progress = stats
            db.commit()
            logger.info(f"  Phase 4 waiting: {stats} ({len(pending)} batches pending)")
            await asyncio.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

    _complete_run(db, run, stats)
    logger.info(f"Phase 4 complete: {stats}")
    return stats


# ── Main orchestrator ─────────────────────────────────────────────────


async def run_pipeline(dry_run: bool = True, resume: bool = False):
    """Run all enrichment phases sequentially."""
    db = SessionLocal()

    try:
        logger.info(f"{'═' * 60}")
        logger.info(f"  ENRICHMENT PIPELINE — {'DRY RUN' if dry_run else 'LIVE'}")
        logger.info(f"  Started: {datetime.now(timezone.utc).isoformat()}")
        logger.info(f"{'═' * 60}")

        # Phase 0: Reclassify coarse categories
        if (
            not resume
            or not db.query(EnrichmentRun)
            .filter(EnrichmentRun.phase == "phase_0_reclassify", EnrichmentRun.status == "completed")
            .first()
        ):
            await phase_0_reclassify(db, dry_run)
        else:
            logger.info("Phase 0 already complete — skipping")

        # Phase 1: Mine sighting data
        if (
            not resume
            or not db.query(EnrichmentRun)
            .filter(EnrichmentRun.phase == "phase_1_sightings", EnrichmentRun.status == "completed")
            .first()
        ):
            await phase_1_mine_sightings(db, dry_run)
        else:
            logger.info("Phase 1 already complete — skipping")

        # Phase 2: AI batch enrichment (Sonnet)
        if (
            not resume
            or not db.query(EnrichmentRun)
            .filter(EnrichmentRun.phase == "phase_2_batch", EnrichmentRun.status == "completed")
            .first()
        ):
            await phase_2_batch_enrichment(db, dry_run)
        else:
            logger.info("Phase 2 already complete — skipping")

        # Phase 3: Structured spec extraction
        await phase_3_spec_extraction(db, dry_run)

        # Phase 4: Premium descriptions
        if (
            not resume
            or not db.query(EnrichmentRun)
            .filter(EnrichmentRun.phase == "phase_4_descriptions", EnrichmentRun.status == "completed")
            .first()
        ):
            await phase_4_premium_descriptions(db, dry_run)
        else:
            logger.info("Phase 4 already complete — skipping")

        # Phase 5: Web search enrichment (lifecycle, RoHS, cross-refs)
        if not dry_run:
            if (
                not resume
                or not db.query(EnrichmentRun)
                .filter(EnrichmentRun.phase == "phase_5_web", EnrichmentRun.status == "completed")
                .first()
            ):
                from scripts.enrich_web_verified import run_web_enrichment

                await run_web_enrichment(db, limit=5000, dry_run=False)
            else:
                logger.info("Phase 5 already complete — skipping")
        else:
            logger.info("Phase 5 skipped in dry-run (web search is real-time only)")

        # Phase 6: Cross-verification
        if not dry_run:
            from scripts.verify_enrichment import run_verification

            report = await run_verification(db, sample_size=1000)
            if report.get("all_targets_met"):
                logger.info("✓ All accuracy targets met!")
            else:
                logger.warning(f"Accuracy targets: {report.get('targets_met', {})}")
        else:
            logger.info("Phase 6 skipped in dry-run")

        logger.info(f"{'═' * 60}")
        logger.info("  PIPELINE COMPLETE")
        logger.info(f"{'═' * 60}")

    except Exception as e:
        logger.error(f"Pipeline error: {e}")
        raise
    finally:
        db.close()


async def run_loop(interval_hours: float = 6.0):
    """Run the enrichment pipeline in a continuous loop.

    After each full run, sleeps for interval_hours then runs again. Always resumes from
    checkpoint so completed phases are skipped. Catches and logs all errors — never
    exits unless killed.
    """
    logger.info(f"Enrichment worker starting — loop interval: {interval_hours}h")

    while True:
        try:
            await run_pipeline(dry_run=False, resume=True)
            logger.info(f"Pipeline run complete — sleeping {interval_hours}h until next run")
        except (KeyboardInterrupt, SystemExit):
            logger.info("Enrichment worker shutting down")
            raise
        except Exception as e:
            logger.error(f"Pipeline run failed: {e} — will retry in {interval_hours}h")

        await asyncio.sleep(interval_hours * 3600)

        # Clear completed runs so the next loop does a fresh pass
        db = SessionLocal()
        try:
            db.query(EnrichmentRun).filter(EnrichmentRun.status == "completed").delete(synchronize_session=False)
            db.commit()
            logger.info("Cleared completed runs — next loop will re-enrich")
        except Exception as e:
            logger.warning(f"Failed to clear completed runs: {e}")
        finally:
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous enrichment pipeline")
    parser.add_argument("--apply", action="store_true", help="Actually write changes (default: dry run)")
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint")
    parser.add_argument("--loop", action="store_true", help="Run continuously (apply + resume + repeat)")
    parser.add_argument("--interval", type=float, default=6.0, help="Hours between loop runs (default: 6)")
    args = parser.parse_args()

    # Log to file so output is visible via docker compose logs or tail
    LOG_FILE = "/tmp/enrichment_pipeline.log"
    logger.add(LOG_FILE, rotation="50 MB", retention="7 days", level="INFO")
    logger.info(f"Logging to {LOG_FILE}")

    if args.loop:
        asyncio.run(run_loop(interval_hours=args.interval))
    else:
        asyncio.run(run_pipeline(dry_run=not args.apply, resume=args.resume))

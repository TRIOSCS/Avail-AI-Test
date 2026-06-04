"""Structured-spec enrichment — second-pass, per-commodity spec extraction.

What: Extracts per-commodity structured specs for material cards via Claude and
      writes them through record_spec (specs_structured JSONB + material_spec_facets).
Called by: jobs/tagging_jobs.py (_job_material_enrichment), routers/htmx_views.py
           (enrich button), app/management/enrich_specs.py (backfill).
Depends on: commodity_registry.get_batch_spec_schema, spec_write_service.record_spec,
            utils.claude_client.claude_structured.
"""

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.services.commodity_registry import get_batch_spec_schema
from app.services.spec_write_service import record_spec

# {category: {"specs": [{"key","label","type","values"?,"canonical_unit"?,"unit_hint"?}]}}
COMMODITY_SPECS = get_batch_spec_schema()

BATCH_SIZE = 25
FACET_MIN_CONF = 0.70
SUMMARY_MIN_CONF = 0.85

_SYSTEM = (
    "You are an expert electronic component engineer. Extract structured "
    "specifications from part numbers and descriptions. Only include specs you are "
    "confident about. Set null for anything uncertain."
)


def build_spec_prompt(category: str, cards: list[dict]) -> str:
    """Build a commodity-specific spec extraction prompt."""
    schema = COMMODITY_SPECS[category]
    spec_instructions = []
    for spec in schema["specs"]:
        line = f"- {spec['key']}: {spec['label']}"
        if spec["type"] == "enum":
            line += f" (one of: {spec.get('values', 'see common values')})"
        elif spec["type"] == "numeric":
            unit = spec.get("unit_hint", "")
            line += f" (number{', unit: ' + unit if unit else ''})"
        elif spec["type"] == "boolean":
            line += " (true/false)"
        spec_instructions.append(line)
    spec_text = "\n".join(spec_instructions)

    card_lines = []
    for c in cards:
        entry = f"- MPN: {c['display_mpn']}"
        if c.get("manufacturer"):
            entry += f" | Mfg: {c['manufacturer']}"
        if c.get("description"):
            entry += f" | Desc: {c['description'][:200]}"
        card_lines.append(entry)
    cards_text = "\n".join(card_lines)

    return (
        f"Extract technical specifications for these {category} components.\n\n"
        f"Specs to extract:\n{spec_text}\n\n"
        f"Components:\n{cards_text}\n\n"
        f"For each component, return its specs. Set null for specs you cannot determine. "
        f"Include a 'confidence' (0.0-1.0) for each spec value."
    )


def build_spec_schema(category: str) -> dict:
    """Build the JSON schema for spec extraction output for a commodity."""
    schema = COMMODITY_SPECS[category]
    spec_props: dict = {}
    for spec in schema["specs"]:
        spec_props[spec["key"]] = {"type": ["string", "number", "boolean", "null"]}
        spec_props[f"{spec['key']}_confidence"] = {"type": "number"}
    return {
        "type": "object",
        "properties": {
            "parts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"mpn": {"type": "string"}, **spec_props},
                    "required": ["mpn"],
                },
            }
        },
        "required": ["parts"],
    }


def specs_to_summary(category: str, ai_part: dict, *, min_conf: float = SUMMARY_MIN_CONF) -> str | None:
    """Convert AI-extracted specs to a parseable "Key: Value | ..." summary string."""
    schema = COMMODITY_SPECS[category]
    parts = []
    for spec in schema["specs"]:
        value = ai_part.get(spec["key"])
        conf = ai_part.get(f"{spec['key']}_confidence", 0.0)
        if value is not None and conf >= min_conf:
            parts.append(f"{spec['label']}: {value}")
    return " | ".join(parts) if parts else None


async def enrich_card_specs(
    card_ids: list[int], db: Session, *, force: bool = False, batch_size: int = BATCH_SIZE
) -> dict:
    """Extract and record structured specs for the given cards (second pass).

    Eligible cards have a category, a non-empty description, and (unless force) no prior
    spec pass. Cards are grouped by category; each category uses its own prompt/schema.
    Specs with confidence >= FACET_MIN_CONF are written via record_spec (JSONB + facet).
    Every processed card gets specs_enriched_at stamped so it is not reprocessed.
    """
    from app.utils.claude_client import claude_structured  # lazy: tests patch at source

    query = db.query(MaterialCard).filter(
        MaterialCard.id.in_(card_ids),
        MaterialCard.deleted_at.is_(None),
        MaterialCard.category.isnot(None),
        MaterialCard.description.isnot(None),
        MaterialCard.description != "",
    )
    if not force:
        query = query.filter(MaterialCard.specs_enriched_at.is_(None))
    cards = query.all()

    stats = {"cards_processed": 0, "specs_written": 0, "cards_with_specs": 0, "errors": 0, "skipped_no_schema": 0}

    by_cat: dict[str, list[MaterialCard]] = {}
    for c in cards:
        by_cat.setdefault((c.category or "").lower().strip(), []).append(c)

    now = datetime.now(timezone.utc)
    for cat, cat_cards in by_cat.items():
        if cat not in COMMODITY_SPECS:
            stats["skipped_no_schema"] += len(cat_cards)
            continue
        json_schema = build_spec_schema(cat)
        spec_defs = COMMODITY_SPECS[cat]["specs"]

        for i in range(0, len(cat_cards), batch_size):
            chunk = cat_cards[i : i + batch_size]
            card_dicts = [
                {"display_mpn": c.display_mpn, "manufacturer": c.manufacturer, "description": c.description}
                for c in chunk
            ]
            prompt = build_spec_prompt(cat, card_dicts)
            try:
                result = await claude_structured(
                    prompt, json_schema, system=_SYSTEM, model_tier="smart", max_tokens=8192, timeout=120
                )
            except Exception as e:  # noqa: BLE001 — isolate one category's failure
                logger.warning("spec extraction failed for category {}: {}", cat, e)
                stats["errors"] += len(chunk)
                continue

            parts = (result or {}).get("parts", [])
            mpn_to_part = {p.get("mpn"): p for p in parts if isinstance(p, dict)}

            for c in chunk:
                # Exact MPN match only — the output schema requires `mpn` per part; a positional
                # fallback could mis-assign one card's specs to another if the AI drops a part.
                ai_part = mpn_to_part.get(c.display_mpn)
                stats["cards_processed"] += 1
                wrote_any = False
                if ai_part:
                    for spec in spec_defs:
                        value = ai_part.get(spec["key"])
                        conf = ai_part.get(f"{spec['key']}_confidence", 0.0)
                        if value is not None and conf >= FACET_MIN_CONF:
                            if record_spec(
                                db,
                                int(c.id),
                                spec["key"],
                                value,
                                source="spec_extraction",
                                confidence=conf,
                                unit=spec.get("canonical_unit"),
                            ):
                                stats["specs_written"] += 1
                                wrote_any = True
                    summary = specs_to_summary(cat, ai_part)
                    if summary:
                        c.specs_summary = summary
                if wrote_any:
                    stats["cards_with_specs"] += 1
                c.specs_enriched_at = now

            try:
                db.commit()
            except Exception as e:  # noqa: BLE001
                logger.error("spec enrichment commit failed for {}: {}", cat, e)
                db.rollback()
                stats["errors"] += len(chunk)

    return stats


async def enrich_pending_specs(db: Session, *, limit: int = 300, batch_size: int = BATCH_SIZE) -> dict:
    """Find and enrich cards that need a spec pass (card-level enriched, no specs
    yet)."""
    rows = (
        db.query(MaterialCard.id)
        .filter(
            MaterialCard.specs_enriched_at.is_(None),
            MaterialCard.category.isnot(None),
            MaterialCard.description.isnot(None),
            MaterialCard.description != "",
            MaterialCard.deleted_at.is_(None),
        )
        .order_by(MaterialCard.search_count.desc().nullslast())
        .limit(limit)
        .all()
    )
    card_ids = [r[0] for r in rows]
    if not card_ids:
        return {"cards_processed": 0, "specs_written": 0, "cards_with_specs": 0, "errors": 0, "skipped_no_schema": 0}
    return await enrich_card_specs(card_ids, db, force=False, batch_size=batch_size)

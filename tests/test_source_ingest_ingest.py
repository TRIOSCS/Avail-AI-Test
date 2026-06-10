"""tests/test_source_ingest_ingest.py — SP-Ingest ingest (AUGMENT via the SP2 ladder).

Covers: app/services/source_ingest/ingest.py — AUGMENT creates a new card with
category+description+specs at tier 95 (trio_source); an existing card's description is NOT
clobbered; dry-run writes nothing and the stats match; trio_source(95) beats an existing
mpn_decode(85) spec; a later lower-tier write can't overwrite trio_source.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas
from app.services.source_ingest.ingest import ingest
from app.services.source_ingest.models import ConsolidatedPart
from app.services.spec_write_service import load_schema_cache, record_spec


def _part(**kw) -> ConsolidatedPart:
    base = dict(normalized_mpn="st4000nm0035", raw_mpn="ST4000NM0035", category="hdd")
    base.update(kw)
    return ConsolidatedPart(**base)


def _facets(db: Session, card_id: int) -> dict:
    rows = db.query(MaterialSpecFacet).filter_by(material_card_id=card_id).all()
    return {r.spec_key: (r.value_text if r.value_text is not None else r.value_numeric) for r in rows}


def test_augment_creates_new_card_with_category_desc_specs(db_session: Session):
    seed_commodity_schemas(db_session)
    part = _part(
        manufacturer="Seagate",
        description="4TB 7.2K SAS 3.5in Enterprise HDD",
        condition="New",
        specs={"capacity_gb": "4000", "form_factor": '3.5"'},
    )
    stats = ingest(db_session, [part], apply=True)

    card = db_session.query(MaterialCard).filter_by(normalized_mpn="st4000nm0035").first()
    assert card is not None
    assert card.display_mpn == "ST4000NM0035"
    assert card.manufacturer == "Seagate"
    assert card.category == "hdd"
    assert card.category_source == "trio_source"
    assert card.category_tier == 95
    assert card.description == "4TB 7.2K SAS 3.5in Enterprise HDD"
    assert card.condition == "New"
    f = _facets(db_session, card.id)
    assert f["capacity_gb"] == 4000
    assert f["form_factor"] == '3.5"'
    assert stats["created"] == 1
    assert stats["specs_written"] == 2
    assert stats["fields_by_source"]["trio_source"] >= 3  # category + desc + condition + specs


def test_existing_description_not_clobbered(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(
        normalized_mpn="st4000nm0035",
        display_mpn="ST4000NM0035",
        description="Existing description — keep me",
        category="hdd",
    )
    db_session.add(card)
    db_session.flush()

    part = _part(description="A different, longer source description that must NOT overwrite")
    stats = ingest(db_session, [part], apply=True)

    db_session.refresh(card)
    assert card.description == "Existing description — keep me"  # not clobbered
    assert stats["updated"] == 1
    assert stats["descriptions_filled"] == 0


def test_dry_run_writes_nothing_and_stats_match(db_session: Session):
    seed_commodity_schemas(db_session)
    part = _part(description="4TB HDD", specs={"capacity_gb": "4000"})
    stats = ingest(db_session, [part], apply=False)

    # No card created.
    assert db_session.query(MaterialCard).filter_by(normalized_mpn="st4000nm0035").first() is None
    assert stats["would_create"] == 1
    assert stats["created"] == 0
    assert stats["categories_set"] == 1
    assert stats["descriptions_filled"] == 1
    assert stats["specs_written"] == 1
    assert len(stats["sample"]) == 1
    assert stats["sample"][0]["action"] == "create"


def test_trio_source_beats_existing_mpn_decode_spec(db_session: Session):
    # Pre-seed a card with an mpn_decode(85) capacity; trio_source(95) must override it.
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="st4000nm0035", display_mpn="ST4000NM0035", category="hdd")
    db_session.add(card)
    db_session.flush()
    cache = load_schema_cache(db_session, "hdd")
    record_spec(db_session, card.id, "capacity_gb", 3000, source="mpn_decode", confidence=0.95, schema_cache=cache)
    db_session.flush()
    assert _facets(db_session, card.id)["capacity_gb"] == 3000

    ingest(db_session, [_part(specs={"capacity_gb": "4000"})], apply=True)
    db_session.refresh(card)
    assert _facets(db_session, card.id)["capacity_gb"] == 4000  # trio_source(95) won
    assert card.specs_structured["capacity_gb"]["source"] == "trio_source"
    assert card.specs_structured["capacity_gb"]["tier"] == 95


def test_later_lower_tier_cannot_overwrite_trio_source(db_session: Session):
    # trio_source(95) lands first; a later mpn_decode(85) write must lose.
    seed_commodity_schemas(db_session)
    ingest(db_session, [_part(specs={"capacity_gb": "4000"})], apply=True)
    card = db_session.query(MaterialCard).filter_by(normalized_mpn="st4000nm0035").first()
    cache = load_schema_cache(db_session, "hdd")

    wrote = record_spec(
        db_session, card.id, "capacity_gb", 8000, source="mpn_decode", confidence=0.99, schema_cache=cache
    )
    db_session.flush()
    assert wrote is False  # lower tier rejected by the ladder
    assert _facets(db_session, card.id)["capacity_gb"] == 4000  # trio_source value intact


def test_ai_inferred_category_uses_trio_source_ai_tier(db_session: Session):
    # No source category, but ai_correct supplied one → written at trio_source_ai (88).
    seed_commodity_schemas(db_session)
    part = _part(category=None, ai_category="hdd", ai_category_confidence=0.9)
    ingest(db_session, [part], apply=True)
    card = db_session.query(MaterialCard).filter_by(normalized_mpn="st4000nm0035").first()
    assert card.category == "hdd"
    assert card.category_source == "trio_source_ai"
    assert card.category_tier == 88


def test_ai_specs_written_at_trio_source_ai(db_session: Session):
    seed_commodity_schemas(db_session)
    part = _part(ai_specs={"rpm": {"value": "7200", "confidence": 0.9}})
    ingest(db_session, [part], apply=True)
    card = db_session.query(MaterialCard).filter_by(normalized_mpn="st4000nm0035").first()
    assert card.specs_structured["rpm"]["source"] == "trio_source_ai"
    assert card.specs_structured["rpm"]["tier"] == 88


# --- Condition: "Unknown" is treated as NULL on both sides (no synthetic occupancy) ---


def test_unknown_condition_never_written(db_session: Session):
    # A consolidated "Unknown" must NOT fill the column: condition has no tier ladder, so
    # a written Unknown would permanently block a later real value.
    seed_commodity_schemas(db_session)
    stats = ingest(db_session, [_part(condition="Unknown")], apply=True)
    card = db_session.query(MaterialCard).filter_by(normalized_mpn="st4000nm0035").first()
    assert card.condition is None
    assert stats["conditions_filled"] == 0


def test_real_condition_replaces_existing_unknown(db_session: Session):
    # An existing "Unknown" on the card counts as empty — a real consolidated condition
    # (e.g. from a later re-run with inventory-sheet data) must not be blocked by it.
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="st4000nm0035", display_mpn="ST4000NM0035", category="hdd", condition="Unknown")
    db_session.add(card)
    db_session.flush()

    stats = ingest(db_session, [_part(condition="Pulled")], apply=True)
    db_session.refresh(card)
    assert card.condition == "Pulled"
    assert stats["conditions_filled"] == 1


def test_real_condition_does_not_overwrite_real_condition(db_session: Session):
    seed_commodity_schemas(db_session)
    card = MaterialCard(normalized_mpn="st4000nm0035", display_mpn="ST4000NM0035", category="hdd", condition="New")
    db_session.add(card)
    db_session.flush()

    stats = ingest(db_session, [_part(condition="Pulled")], apply=True)
    db_session.refresh(card)
    assert card.condition == "New"  # fill-only-when-empty
    assert stats["conditions_filled"] == 0


# --- Failure isolation: per-part SAVEPOINT contract (mirrors mpn_decoder/writer.py) ---


def test_failed_part_is_isolated_counted_and_siblings_ingest(db_session: Session, monkeypatch):
    # One raising part must: (a) not poison the outer transaction for its siblings,
    # (b) leave NO trace of its own card (savepoint rollback), and (c) be COUNTED in
    # stats["failed"] + failed_mpns so the report cannot silently shrink.
    import app.services.source_ingest.ingest as mod

    seed_commodity_schemas(db_session)
    real_record_spec = mod.record_spec

    def bomb(db, card_id, key, value, **kw):
        card = db.get(MaterialCard, card_id)
        if card is not None and card.normalized_mpn == "badpart":
            raise RuntimeError("flush-level boom")
        return real_record_spec(db, card_id, key, value, **kw)

    monkeypatch.setattr(mod, "record_spec", bomb)

    bad = _part(normalized_mpn="badpart", raw_mpn="BADPART", specs={"capacity_gb": "1"})
    good = _part(description="4TB HDD", condition="New", specs={"capacity_gb": "4000"})
    stats = ingest(db_session, [bad, good], apply=True)

    assert stats["parts_seen"] == 2
    assert stats["failed"] == 1
    assert stats["failed_mpns"] == ["badpart"]
    assert stats["created"] == 1  # the failed part is NOT in created
    # The bad part's card was rolled back wholesale; the good sibling persisted fully.
    assert db_session.query(MaterialCard).filter_by(normalized_mpn="badpart").first() is None
    good_card = db_session.query(MaterialCard).filter_by(normalized_mpn="st4000nm0035").first()
    assert good_card is not None
    assert good_card.condition == "New"
    assert _facets(db_session, good_card.id)["capacity_gb"] == 4000


def test_rolled_back_card_contributes_nothing_to_tallies(db_session: Session, monkeypatch):
    # Counters must merge AFTER a clean savepoint release: a card whose write raises
    # midway (category already "set" in the savepoint) must not inflate categories_set.
    import app.services.source_ingest.ingest as mod

    seed_commodity_schemas(db_session)

    def bomb(db, card_id, key, value, **kw):
        raise RuntimeError("boom after category was set in the savepoint")

    monkeypatch.setattr(mod, "record_spec", bomb)
    stats = ingest(db_session, [_part(description="x", condition="New", specs={"capacity_gb": "1"})], apply=True)
    assert stats["failed"] == 1
    # Everything the savepoint rolled back is absent from the report.
    assert stats["categories_set"] == 0
    assert stats["descriptions_filled"] == 0
    assert stats["conditions_filled"] == 0
    assert stats["specs_written"] == 0
    assert stats["fields_by_source"] == {}


# --- Dry-run / apply parity: the dry run is the operator's go/no-go gate ---


def _parity_keys():
    return ("categories_set", "descriptions_filled", "conditions_filled", "specs_written", "fields_by_source")


def test_dry_run_matches_apply_on_existing_card_with_gates(db_session: Session):
    # Existing card carrying (a) a HIGHER-tier spec the part loses to (manual=100),
    # (b) a spec key with NO hdd schema ("cpu"), and (c) a category the part's
    # trio_source(95) write WOULD win against (vendor 90 — same value, provenance
    # refresh). Dry-run tallies must equal apply tallies exactly.
    seed_commodity_schemas(db_session)
    card = MaterialCard(
        normalized_mpn="st4000nm0035",
        display_mpn="ST4000NM0035",
        category="hdd",
        category_source="digikey_api",
        category_confidence=0.9,
        category_tier=90,
        specs_structured={
            "capacity_gb": {
                "value": 9999,
                "source": "manual",
                "confidence": 1.0,
                "tier": 100,
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        },
    )
    db_session.add(card)
    db_session.flush()

    part = _part(
        description="4TB Enterprise HDD",
        condition="Pulled",
        specs={"capacity_gb": "4000", "cpu": "Xeon 6230", "form_factor": '3.5"'},
    )

    dry = ingest(db_session, [part], apply=False)
    # Dry run wrote nothing — same starting state for apply.
    assert card.category_source == "digikey_api"
    applied = ingest(db_session, [part], apply=True)

    assert dry["would_update"] == applied["updated"] == 1
    for key in _parity_keys():
        assert dry[key] == applied[key], f"dry/apply diverged on {key}: {dry[key]} != {applied[key]}"
    # The gates actually engaged: capacity lost to manual, cpu had no schema → only
    # form_factor counted; category won (95 > 90); condition filled.
    assert dry["specs_written"] == 1
    assert dry["categories_set"] == 1
    assert dry["conditions_filled"] == 1


def test_dry_run_matches_apply_on_category_flip_with_purge(db_session: Session):
    # The part re-categorizes a low-tier dram card to hdd (95 > 40): apply purges the old
    # commodity's facet/JSONB entries (set_category) and writes the hdd specs — the dry
    # run must predict the same tallies, and apply must actually purge.
    seed_commodity_schemas(db_session)
    card = MaterialCard(
        normalized_mpn="st4000nm0035",
        display_mpn="ST4000NM0035",
        category="dram",
        category_source="claude_opus_inferred",
        category_confidence=0.5,
        category_tier=40,
    )
    db_session.add(card)
    db_session.flush()
    cache = load_schema_cache(db_session, "dram")
    assert record_spec(
        db_session, card.id, "ddr_type", "DDR4", source="mpn_decode", confidence=0.95, schema_cache=cache
    )
    db_session.flush()

    part = _part(specs={"capacity_gb": "4000"})

    dry = ingest(db_session, [part], apply=False)
    applied = ingest(db_session, [part], apply=True)

    assert dry["would_update"] == applied["updated"] == 1
    for key in _parity_keys():
        assert dry[key] == applied[key], f"dry/apply diverged on {key}: {dry[key]} != {applied[key]}"
    db_session.refresh(card)
    assert card.category == "hdd"
    assert "ddr_type" not in (card.specs_structured or {})  # old commodity purged
    assert _facets(db_session, card.id) == {"capacity_gb": 4000}


def test_dry_run_does_not_count_specs_without_resolvable_category(db_session: Session):
    # A part with NO category (and no AI category) cannot write any spec on --apply
    # (record_spec requires a category) — the dry run must count ZERO, not len(specs).
    seed_commodity_schemas(db_session)
    part = _part(category=None, specs={"capacity_gb": "4000", "rpm": "7200"})
    dry = ingest(db_session, [part], apply=False)
    applied = ingest(db_session, [part], apply=True)
    assert dry["specs_written"] == applied["specs_written"] == 0
    assert dry["categories_set"] == applied["categories_set"] == 0

"""tests/test_spec_write_conflicts.py — validation-conflict contract (on-add
enrichment).

Covers: spec_tiers.record_validation_conflict / clear_validation_conflicts, the
record_spec lose-branch hook, the _set_provenanced_column lose-branch hook (category
AND brand/manufacturer — the hook covers every provenanced column), manual/100 apex
survival across every lower tier, (key, source) de-dupe, equal-value no-op, and
clearing via the PUT update + conflict-accept routes.
Depends on: conftest.py (db_session, client), spec_write_service, spec_tiers.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard
from app.services.spec_tiers import (
    SOURCE_TIER,
    clear_validation_conflicts,
    set_category,
    tier_for,
)
from app.services.spec_write_service import record_spec


def _make_card(db: Session, mpn: str = "CONFLICT-001", category: str = "dram") -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer="TestCo",
        category=category,
        created_at=datetime.now(timezone.utc),
    )
    db.add(card)
    db.flush()
    return card


def _make_schema(db: Session, commodity: str = "dram", spec_key: str = "ddr_type", **kwargs) -> CommoditySpecSchema:
    defaults = dict(
        commodity=commodity,
        spec_key=spec_key,
        display_name=spec_key.replace("_", " ").title(),
        data_type="enum",
        enum_values=["DDR3", "DDR4", "DDR5"],
        sort_order=0,
        is_filterable=True,
        is_primary=False,
    )
    defaults.update(kwargs)
    schema = CommoditySpecSchema(**defaults)
    db.add(schema)
    db.flush()
    return schema


_LOWER_SOURCES = sorted(s for s, t in SOURCE_TIER.items() if t < 100)


# --- Manual apex survival + flagging band ------------------------------------


@pytest.mark.parametrize("source", _LOWER_SOURCES)
def test_manual_survives_every_lower_tier_write(db_session: Session, source: str):
    """A manual (tier 100) spec value survives a write from EVERY lower-tier source, and
    the conflict flag raises iff the loser sits in the authoritative band (tier >= 80) —
    web/brokerbin/AI tiers never flag."""
    card = _make_card(db_session)
    _make_schema(db_session)

    assert record_spec(db_session, card.id, "ddr_type", "DDR3", source="manual", confidence=1.0)
    assert not record_spec(db_session, card.id, "ddr_type", "DDR4", source=source, confidence=0.99)

    db_session.flush()
    assert card.specs_structured["ddr_type"]["value"] == "DDR3"
    assert card.specs_structured["ddr_type"]["source"] == "manual"
    expected_flag = tier_for(source) >= 80
    assert bool(card.has_validation_conflict) is expected_flag
    if expected_flag:
        entries = [c for c in card.validation_conflicts if c["key"] == "ddr_type"]
        assert len(entries) == 1
        assert entries[0]["evidence"]["source"] == source
    else:
        assert not (card.validation_conflicts or [])


def test_conflict_entry_shape(db_session: Session):
    """The persisted conflict entry carries the full spec'd shape."""
    card = _make_card(db_session)
    _make_schema(db_session)

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="manual", confidence=1.0)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.97)

    db_session.flush()
    (entry,) = card.validation_conflicts
    assert entry["key"] == "ddr_type"
    assert entry["manual"]["value"] == "DDR3"
    assert entry["manual"]["updated_at"]  # ISO stamp from the manual write
    ev = entry["evidence"]
    assert ev["source"] == "digikey_api"
    assert ev["tier"] == 90
    assert ev["confidence"] == 0.97
    assert ev["value"] == "DDR4"
    assert ev["observed_at"]


def test_conflict_dedupe_per_key_and_source(db_session: Session):
    """One entry per (key, evidence.source) — newest evidence replaces."""
    card = _make_card(db_session)
    _make_schema(db_session)

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="manual", confidence=1.0)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.90)
    record_spec(db_session, card.id, "ddr_type", "DDR5", source="digikey_api", confidence=0.95)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="mouser_api", confidence=0.92)

    db_session.flush()
    entries = [c for c in card.validation_conflicts if c["key"] == "ddr_type"]
    assert len(entries) == 2  # digikey_api (replaced) + mouser_api
    digikey = next(e for e in entries if e["evidence"]["source"] == "digikey_api")
    assert digikey["evidence"]["value"] == "DDR5"  # newest replaced the older entry


def test_equal_value_records_no_conflict(db_session: Session):
    """Corroboration is not stored — same normalized value never flags."""
    card = _make_card(db_session)
    _make_schema(db_session)

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="manual", confidence=1.0)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.99)

    db_session.flush()
    assert not (card.validation_conflicts or [])
    assert not card.has_validation_conflict


def test_same_source_corroboration_drops_stale_entry(db_session: Session):
    """'Newest evidence replaces' includes replacing-with-nothing: when a source's
    latest observation AGREES with the manual value, its stale contradiction is removed
    (deterministic sources re-fire every pass — a fixed decoder must unflag the
    card)."""
    card = _make_card(db_session)
    _make_schema(db_session)

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="manual", confidence=1.0)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.9)
    db_session.flush()
    assert card.has_validation_conflict

    # The same source now reports the manual value — corroboration clears its entry.
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="digikey_api", confidence=0.95)
    db_session.flush()
    assert (card.validation_conflicts or []) == []
    assert not card.has_validation_conflict


def test_same_source_corroboration_keeps_other_sources_entries(db_session: Session):
    """Corroboration from ONE source only removes THAT source's stale entry — another
    source's live contradiction stays and the flag recomputes True."""
    card = _make_card(db_session)
    _make_schema(db_session)

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="manual", confidence=1.0)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.9)
    record_spec(db_session, card.id, "ddr_type", "DDR5", source="mouser_api", confidence=0.9)
    db_session.flush()
    assert len(card.validation_conflicts) == 2

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="digikey_api", confidence=0.95)
    db_session.flush()
    assert [c["evidence"]["source"] for c in card.validation_conflicts] == ["mouser_api"]
    assert card.has_validation_conflict


def test_non_manual_existing_never_flags(db_session: Session):
    """Only manual values raise conflicts — a vendor value beaten by another vendor
    write is plain ladder arbitration, not a review item."""
    card = _make_card(db_session)
    _make_schema(db_session)

    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.99)
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="mpn_decode", confidence=0.95)  # loses (85 < 90)

    db_session.flush()
    assert not (card.validation_conflicts or [])
    assert not card.has_validation_conflict


# --- set_category hook (V2: category vs decode commodity) ---------------------


def test_set_category_conflict_on_manual_vs_decode(db_session: Session):
    """A regex-gated decode commodity that loses to a manual category records a conflict
    on key='category'."""
    card = _make_card(db_session, category=None)
    assert set_category(card, "dram", "manual", 1.0)
    assert not set_category(card, "hdd", "mpn_decode", 0.95)

    assert card.category == "dram"
    (entry,) = card.validation_conflicts
    assert entry["key"] == "category"
    assert entry["manual"]["value"] == "dram"
    assert entry["evidence"] == {
        **entry["evidence"],
        "source": "mpn_decode",
        "tier": 85,
        "value": "hdd",
    }
    assert card.has_validation_conflict


def test_set_category_low_tier_loss_never_flags(db_session: Session):
    """An ai_guess (tier 40) losing to a manual category does NOT flag."""
    card = _make_card(db_session, category=None)
    set_category(card, "dram", "manual", 1.0)
    set_category(card, "hdd", "ai_guess", 0.99)

    assert card.category == "dram"
    assert not (card.validation_conflicts or [])
    assert not card.has_validation_conflict


def test_set_category_dry_run_never_mutates_conflicts(db_session: Session):
    """Write=False is the read-only twin — no conflict entries either."""
    card = _make_card(db_session, category=None)
    set_category(card, "dram", "manual", 1.0)
    set_category(card, "hdd", "mpn_decode", 0.95, write=False)

    assert not (card.validation_conflicts or [])
    assert not card.has_validation_conflict


# --- Clearing ------------------------------------------------------------------


def test_clear_validation_conflicts_toggles_flag(db_session: Session):
    card = _make_card(db_session, category=None)
    set_category(card, "dram", "manual", 1.0)
    set_category(card, "hdd", "mpn_decode", 0.95)
    assert card.has_validation_conflict

    assert clear_validation_conflicts(card, "category")
    assert card.validation_conflicts == []
    assert not card.has_validation_conflict
    # idempotent — nothing left to clear
    assert not clear_validation_conflicts(card, "category")


def test_clear_only_named_key(db_session: Session):
    """Clearing one key keeps the flag while other keys still hold entries."""
    card = _make_card(db_session)
    _make_schema(db_session)
    _make_schema(db_session, spec_key="form_factor", enum_values=["RDIMM", "UDIMM"])

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="manual", confidence=1.0)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.9)
    record_spec(db_session, card.id, "form_factor", "RDIMM", source="manual", confidence=1.0)
    record_spec(db_session, card.id, "form_factor", "UDIMM", source="mpn_decode", confidence=0.95)
    db_session.flush()
    assert len(card.validation_conflicts) == 2

    clear_validation_conflicts(card, "ddr_type")
    assert [c["key"] for c in card.validation_conflicts] == ["form_factor"]
    assert card.has_validation_conflict


def test_put_update_clears_category_conflict(client, db_session: Session):
    """A PUT carrying the category re-asserts it — the conflict clears even when the
    value is unchanged."""
    card = _make_card(db_session, category=None)
    set_category(card, "dram", "manual", 1.0)
    set_category(card, "hdd", "mpn_decode", 0.95)
    db_session.commit()
    assert card.has_validation_conflict

    resp = client.put(f"/api/materials/{card.id}", json={"category": "dram"})
    assert resp.status_code == 200
    db_session.flush()
    assert card.category == "dram"
    assert not card.has_validation_conflict
    assert card.validation_conflicts == []


def test_accept_route_writes_manual_and_clears_category(client, db_session: Session):
    """POST .../conflicts/category/accept adopts the evidence value at manual/100 and
    clears the key's entries."""
    card = _make_card(db_session, category=None)
    set_category(card, "dram", "manual", 1.0)
    set_category(card, "hdd", "mpn_decode", 0.95)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/materials/{card.id}/conflicts/category/accept",
        data={"source": "mpn_decode"},
    )
    assert resp.status_code == 200
    db_session.flush()
    assert card.category == "hdd"
    assert card.category_source == "manual"  # a human decision
    assert card.category_tier == 100
    assert not card.has_validation_conflict
    assert card.validation_conflicts == []


def test_accept_route_writes_manual_spec_key(client, db_session: Session):
    """Accepting a spec-key conflict writes the evidence value through record_spec at
    manual/100."""
    card = _make_card(db_session)
    _make_schema(db_session)
    record_spec(db_session, card.id, "ddr_type", "DDR3", source="manual", confidence=1.0)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.97)
    db_session.commit()

    resp = client.post(f"/v2/partials/materials/{card.id}/conflicts/ddr_type/accept")
    assert resp.status_code == 200
    db_session.flush()
    assert card.specs_structured["ddr_type"]["value"] == "DDR4"
    assert card.specs_structured["ddr_type"]["source"] == "manual"
    assert card.specs_structured["ddr_type"]["tier"] == 100
    assert not card.has_validation_conflict


def test_accept_route_404_when_no_conflict(client, db_session: Session):
    card = _make_card(db_session)
    db_session.commit()
    resp = client.post(f"/v2/partials/materials/{card.id}/conflicts/ddr_type/accept")
    assert resp.status_code == 404


def test_accept_route_keeps_entry_when_spec_write_fails(client, db_session: Session):
    """'Use this value' must NOT clear the conflict when the write no-ops (the entry is
    the only persisted record of the contradiction) — it keeps it and surfaces a
    toast."""
    card = _make_card(db_session)
    _make_schema(db_session)  # enum: DDR3/DDR4/DDR5 — "DDR9" fails validation
    card.validation_conflicts = [
        {
            "key": "ddr_type",
            "manual": {"value": "DDR3", "updated_at": ""},
            "evidence": {"source": "digikey_api", "tier": 90, "confidence": 0.97, "value": "DDR9", "observed_at": ""},
        }
    ]
    card.has_validation_conflict = True
    db_session.commit()

    resp = client.post(f"/v2/partials/materials/{card.id}/conflicts/ddr_type/accept")
    assert resp.status_code == 200
    assert "showToast" in resp.headers.get("HX-Trigger", "")
    db_session.refresh(card)
    assert card.has_validation_conflict  # entry kept — nothing was written
    assert [c["key"] for c in card.validation_conflicts] == ["ddr_type"]
    assert "ddr_type" not in (card.specs_structured or {})


def test_accept_route_keeps_entry_when_category_write_fails(client, db_session: Session):
    """Category variant: an off-vocab evidence value can't be written by set_category —
    the conflict entry survives and the failure is surfaced."""
    card = _make_card(db_session, category=None)
    set_category(card, "dram", "manual", 1.0)
    card.validation_conflicts = [
        {
            "key": "category",
            "manual": {"value": "dram", "updated_at": ""},
            "evidence": {
                "source": "mpn_decode",
                "tier": 85,
                "confidence": 0.95,
                "value": "flux_capacitor",
                "observed_at": "",
            },
        }
    ]
    card.has_validation_conflict = True
    db_session.commit()

    resp = client.post(f"/v2/partials/materials/{card.id}/conflicts/category/accept")
    assert resp.status_code == 200
    assert "showToast" in resp.headers.get("HX-Trigger", "")
    db_session.refresh(card)
    assert card.category == "dram"  # untouched
    assert card.has_validation_conflict
    assert [c["key"] for c in card.validation_conflicts] == ["category"]


# --- Commodity flip purges orphaned conflict entries -----------------------------


def test_category_flip_purges_orphaned_spec_conflicts(db_session: Session):
    """A category flip purges the old commodity's specs/facets — conflict entries keyed
    by those purged spec keys go with them (the manual values they reference no longer
    exist; accepting one could never write).

    'category' entries survive the purge.
    """
    card = _make_card(db_session, category=None)
    _make_schema(db_session)
    set_category(card, "dram", "manual", 1.0)
    set_category(card, "ssd", "mpn_decode", 0.95)  # loses → category conflict recorded

    record_spec(db_session, card.id, "ddr_type", "DDR3", source="manual", confidence=1.0)
    record_spec(db_session, card.id, "ddr_type", "DDR4", source="digikey_api", confidence=0.9)
    db_session.flush()
    assert {c["key"] for c in card.validation_conflicts} == {"category", "ddr_type"}

    # Manual flip to hdd: dram facets/specs purge → the ddr_type conflict is an orphan.
    assert set_category(card, "hdd", "manual", 1.0)
    db_session.flush()
    assert card.category == "hdd"
    assert "ddr_type" not in (card.specs_structured or {})
    assert [c["key"] for c in card.validation_conflicts] == ["category"]
    assert card.has_validation_conflict  # the surviving category entry keeps the flag

    # And when the purge removes the LAST entry, the flag drops too.
    clear_validation_conflicts(card, "category")
    assert not card.has_validation_conflict


# --- Brand / manufacturer (the hook lives in _set_provenanced_column, so the same
# --- contract covers every provenanced column, not just category) ---------------


def test_set_manufacturer_conflict_on_manual_vs_decode(db_session: Session):
    """An authoritative decode maker that loses to a manual manufacturer records a
    conflict on key='manufacturer' — same contract as category."""
    from app.services.spec_tiers import set_manufacturer

    card = _make_card(db_session)
    assert set_manufacturer(card, "Seagate Technology", "manual", 1.0)
    assert not set_manufacturer(card, "Samsung", "mpn_decode", 0.95)

    assert card.manufacturer == "Seagate Technology"
    (entry,) = card.validation_conflicts
    assert entry["key"] == "manufacturer"
    assert entry["manual"]["value"] == "Seagate Technology"
    assert entry["evidence"] == {
        **entry["evidence"],
        "source": "mpn_decode",
        "tier": 85,
        "value": "Samsung",
    }
    assert card.has_validation_conflict


def test_set_brand_conflict_on_manual_vs_desc_parse(db_session: Session):
    """A desc_parse (tier 83, authoritative band) brand that loses to a manual brand
    records a conflict on key='brand'."""
    from app.services.spec_tiers import set_brand

    card = _make_card(db_session)
    assert set_brand(card, "IBM", "manual", 1.0)
    assert not set_brand(card, "Dell", "desc_parse", 0.85)

    assert card.brand == "IBM"
    (entry,) = card.validation_conflicts
    assert entry["key"] == "brand"
    assert entry["manual"]["value"] == "IBM"
    assert entry["evidence"]["source"] == "desc_parse"
    assert card.has_validation_conflict


def test_set_manufacturer_low_tier_loss_never_flags(db_session: Session):
    """An ai_guess (tier 40) losing to a manual manufacturer does NOT flag."""
    from app.services.spec_tiers import set_manufacturer

    card = _make_card(db_session)
    set_manufacturer(card, "Seagate Technology", "manual", 1.0)
    set_manufacturer(card, "Samsung", "ai_guess", 0.99)

    assert card.manufacturer == "Seagate Technology"
    assert not (card.validation_conflicts or [])
    assert not card.has_validation_conflict


def test_set_manufacturer_dry_run_never_mutates_conflicts(db_session: Session):
    """Write=False is the read-only twin — no conflict entries either."""
    from app.services.spec_tiers import set_manufacturer

    card = _make_card(db_session)
    set_manufacturer(card, "Seagate Technology", "manual", 1.0)
    set_manufacturer(card, "Samsung", "mpn_decode", 0.95, write=False)

    assert not (card.validation_conflicts or [])
    assert not card.has_validation_conflict


def test_legacy_unprovenanced_manufacturer_never_flags(db_session: Session):
    """_make_card's direct manufacturer write has NULL provenance (legacy floor 50,
    source NULL ≠ 'manual') — a write that loses against it must NOT flag.

    (Only low-tier writes can lose to the floor; both gates reject here.)
    """
    from app.services.spec_tiers import set_manufacturer

    card = _make_card(db_session)  # manufacturer="TestCo", no provenance
    set_manufacturer(card, "Samsung", "ai_guess", 0.2)  # loses to the legacy floor

    assert card.manufacturer == "TestCo"
    assert not (card.validation_conflicts or [])
    assert not card.has_validation_conflict


def test_put_update_clears_manufacturer_conflict(client, db_session: Session):
    """A PUT carrying the manufacturer re-asserts it — the conflict clears even when the
    value is unchanged (same clearing contract as category)."""
    from app.services.spec_tiers import set_manufacturer

    card = _make_card(db_session)
    set_manufacturer(card, "Seagate Technology", "manual", 1.0)
    set_manufacturer(card, "Samsung", "mpn_decode", 0.95)  # loses, records the conflict
    db_session.commit()
    assert card.has_validation_conflict

    resp = client.put(f"/api/materials/{card.id}", json={"manufacturer": "Seagate Technology"})
    assert resp.status_code == 200
    db_session.flush()
    assert card.manufacturer == "Seagate Technology"
    assert not card.has_validation_conflict
    assert card.validation_conflicts == []


def test_accept_route_writes_manual_manufacturer_and_clears(client, db_session: Session):
    """POST .../conflicts/manufacturer/accept adopts the evidence maker at manual/100
    via set_manufacturer (NOT record_spec — there is no spec schema for the dual-brand
    columns) and clears the key's entries."""
    from app.services.spec_tiers import set_manufacturer

    card = _make_card(db_session)
    set_manufacturer(card, "Seagate Technology", "manual", 1.0)
    set_manufacturer(card, "Samsung", "mpn_decode", 0.95)  # loses, records the conflict
    db_session.commit()

    resp = client.post(
        f"/v2/partials/materials/{card.id}/conflicts/manufacturer/accept",
        data={"source": "mpn_decode"},
    )
    assert resp.status_code == 200
    db_session.flush()
    assert card.manufacturer == "Samsung"
    assert card.manufacturer_source == "manual"  # a human decision
    assert card.manufacturer_tier == 100
    assert not card.has_validation_conflict
    assert card.validation_conflicts == []

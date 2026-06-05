"""tests/test_commodity_seeds_coverage.py -- Coverage tests for commodity seed data.

What: Asserts app/data/commodity_seeds.json fully covers the COMMODITY_TREE
      taxonomy and that every seeded spec is well-formed (required keys, valid
      data_type, enum/numeric invariants, unique spec_keys, a primary per
      commodity). Also exercises the idempotent seeder against the test DB.
Called by: pytest (CI + local).
Depends on: conftest.py (db_session fixture, engine), commodity_registry,
            CommoditySpecSchema model.
"""

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema
from app.services.commodity_registry import (
    COMMODITY_TREE,
    _load_commodity_seeds,
    get_all_commodities,
    seed_commodity_schemas,
)
from tests.conftest import engine  # noqa: F401

_VALID_DATA_TYPES = {"numeric", "enum", "boolean"}

SEEDS = _load_commodity_seeds()


def test_every_tree_subcategory_is_seeded():
    """Every COMMODITY_TREE sub-category key has a non-empty spec list."""
    for commodity in get_all_commodities():
        assert commodity in SEEDS, f"{commodity} from COMMODITY_TREE not in seeds"
        assert SEEDS[commodity], f"{commodity} has an empty spec list"


def test_no_orphan_seed_commodities():
    """Every seeded commodity maps to a real COMMODITY_TREE sub-category."""
    tree_keys = set(get_all_commodities())
    for commodity in SEEDS:
        assert commodity in tree_keys, f"{commodity} seeded but absent from COMMODITY_TREE"


def test_commodity_keys_are_lowercased():
    """Seed keys must match COMMODITY_TREE keys exactly (lowercase)."""
    for commodity in SEEDS:
        assert commodity == commodity.lower(), f"{commodity} is not lowercase"


def test_every_spec_has_required_fields():
    """Each spec carries spec_key, display_name, and a valid data_type."""
    for commodity, specs in SEEDS.items():
        for spec in specs:
            assert spec.get("spec_key"), f"{commodity}: spec missing spec_key"
            assert spec.get("display_name"), f"{commodity}/{spec.get('spec_key')}: no display_name"
            assert spec.get("data_type") in _VALID_DATA_TYPES, (
                f"{commodity}/{spec['spec_key']}: bad data_type {spec.get('data_type')!r}"
            )


def test_enum_values_when_present_are_nonempty_lists():
    """When an enum declares enum_values it must be a non-empty list.

    Open-ended enums (e.g. connectors/series, cpu/socket) intentionally omit enum_values
    so the UI accepts free-text, so presence is not required; but a declared list must
    never be empty or the wrong type.
    """
    for commodity, specs in SEEDS.items():
        for spec in specs:
            if spec["data_type"] == "enum" and "enum_values" in spec:
                values = spec["enum_values"]
                assert isinstance(values, list), f"{commodity}/{spec['spec_key']}: enum_values must be a list"
                assert values, f"{commodity}/{spec['spec_key']}: enum_values is empty"


def test_numeric_specs_have_unit_and_canonical_unit():
    """Every numeric spec carries both unit and canonical_unit."""
    for commodity, specs in SEEDS.items():
        for spec in specs:
            if spec["data_type"] == "numeric":
                assert spec.get("unit"), f"{commodity}/{spec['spec_key']}: numeric without unit"
                assert spec.get("canonical_unit"), f"{commodity}/{spec['spec_key']}: numeric without canonical_unit"


def test_spec_keys_unique_within_commodity():
    """No duplicate spec_key inside a single commodity."""
    for commodity, specs in SEEDS.items():
        keys = [spec["spec_key"] for spec in specs]
        assert len(keys) == len(set(keys)), f"{commodity} has duplicate spec_keys: {keys}"


def test_at_least_one_primary_per_commodity():
    """Each commodity flags at least one is_primary spec (for chip rendering)."""
    for commodity, specs in SEEDS.items():
        primaries = [spec for spec in specs if spec.get("is_primary")]
        assert primaries, f"{commodity} has no is_primary spec"


def test_spec_count_within_design_bounds():
    """Per design rules, keep 1..12 specs per commodity (catch-alls may be small)."""
    for commodity, specs in SEEDS.items():
        assert 1 <= len(specs) <= 12, f"{commodity} has {len(specs)} specs (expected 1..12)"


def test_canonical_unit_identity_outside_normalizer_families():
    """Numeric units outside a known normalizer family must self-canonicalize.

    A unit the normalizer cannot convert must set canonical_unit == unit, else the facet
    pipeline emits spurious unit_normalizer warnings. Units that *are* in a family may
    legitimately pick their own display canonical (e.g. cpu clock in GHz rather than the
    family's MHz), so that direction is not enforced here.
    """
    from app.services.unit_normalizer import _CONVERSIONS

    # from_unit (lowercased) -> canonical it would resolve to
    family_canonical = {frm: to for (frm, to) in _CONVERSIONS}

    for commodity, specs in SEEDS.items():
        for spec in specs:
            if spec["data_type"] != "numeric":
                continue
            unit = spec["unit"]
            canonical = spec["canonical_unit"]
            if family_canonical.get(unit.lower()) is None:
                assert canonical == unit, (
                    f"{commodity}/{spec['spec_key']}: unit {unit!r} not in a normalizer "
                    f"family, canonical_unit must equal unit, got {canonical!r}"
                )


def test_seed_commodity_schemas_inserts_new_commodities(db_session: Session):
    """Seeder inserts rows and a sampled new commodity has its primary spec."""
    before = db_session.query(CommoditySpecSchema).count()
    inserted = seed_commodity_schemas(db_session)

    assert inserted > 0
    after = db_session.query(CommoditySpecSchema).count()
    assert after == before + inserted

    # Sampled net-new commodities seed their primary specs.
    leds_primary = db_session.query(CommoditySpecSchema).filter_by(commodity="leds", spec_key="color").one()
    assert leds_primary.is_primary is True
    assert leds_primary.data_type == "enum"
    assert leds_primary.enum_values

    relays_primary = db_session.query(CommoditySpecSchema).filter_by(commodity="relays", spec_key="relay_type").one()
    assert relays_primary.is_primary is True


def test_seed_commodity_schemas_covers_full_tree(db_session: Session):
    """After seeding, every COMMODITY_TREE sub-category has rows in the DB."""
    seed_commodity_schemas(db_session)
    seeded_commodities = {row[0] for row in db_session.query(CommoditySpecSchema.commodity).distinct().all()}
    for group_subs in COMMODITY_TREE.values():
        for commodity in group_subs:
            assert commodity in seeded_commodities, f"{commodity} not seeded into DB"

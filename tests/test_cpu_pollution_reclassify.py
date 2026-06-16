"""tests/test_cpu_pollution_reclassify.py — re-classify the recognizable pollution in
the `cpu` catch-all bucket to the correct commodity via deterministic MPN prefixes.

Depends on: conftest.py (db_session), seed_commodity_schemas, MaterialCard,
spec_tiers.SOURCE_TIER (cpu_pollution_fix=96), commodity_registry.CANONICAL_COMMODITY_KEYS.
"""

from app.services.spec_tiers import SOURCE_TIER, tier_for


def test_cpu_pollution_fix_registered_at_tier_96():
    assert SOURCE_TIER["cpu_pollution_fix"] == 96
    assert tier_for("cpu_pollution_fix") == 96
    # Beats the trio_source 'cpu' default (95), loses to manual (100).
    assert SOURCE_TIER["cpu_pollution_fix"] > SOURCE_TIER["trio_source"]
    assert SOURCE_TIER["cpu_pollution_fix"] < SOURCE_TIER["manual"]


import pytest

from app.services.commodity_registry import CANONICAL_COMMODITY_KEYS
from app.services.cpu_pollution.classifier import classify_polluted_mpn
from app.services.cpu_pollution.prefix_map import PREFIX_RULES


def test_every_prefix_rule_targets_valid_vocab():
    for _pattern, commodity in PREFIX_RULES:
        assert commodity in CANONICAL_COMMODITY_KEYS, f"{commodity} not a canonical commodity"


@pytest.mark.parametrize(
    "mpn,expected",
    [
        ("5-1437720-3", "connectors"),  # TE Connectivity
        ("1437259-6", "connectors"),  # TE Connectivity
        ("SSW-114-22-S-S-VS-P-TR", "connectors"),  # Samtec
        ("CLT-110-02-G-D-BE-A-K-TR", "connectors"),  # Samtec
        ("NRWA330M63V6.3X11TBF", "capacitors"),  # Nichicon
        ("TAJD475K050RNJ", "capacitors"),  # AVX tantalum
        ("B32520C474K189", "capacitors"),  # EPCOS film cap
        ("BLM21AJ601SN1D", "inductors"),  # Murata ferrite bead
        ("CRCW12102K21FKEA", "resistors"),  # Vishay
        ("CD74HC123EE4", "logic_ic"),  # TI CD74 logic
        ("74AUP2G14DW-7", "logic_ic"),  # 74-series logic
        ("BCM5488SA7IPBG", "logic_ic"),  # Broadcom
    ],
)
def test_known_pollution_classifies(mpn, expected):
    assert classify_polluted_mpn(mpn) == expected


@pytest.mark.parametrize(
    "cpu_mpn",
    [
        "SR3QS",
        "SL5CH",  # Intel sSpec
        "CM8068403654318",  # Intel ordering code
        "BX8070110700K",  # Intel boxed
        "CD8069504194701",  # Intel ordering — must NOT collide with CD74 logic
        "E5-2680V4",  # Intel model string
        "100-000000053",  # AMD OPN
        "EPYC 7742",  # AMD model word
    ],
)
def test_real_cpu_is_never_reclassified(cpu_mpn):
    assert classify_polluted_mpn(cpu_mpn) is None


@pytest.mark.parametrize(
    "oem_spare",
    [
        "726719-001",  # HP CPU spare (6-digit core + 3-digit suffix) — NOT a TE connector
        "619559-001",  # HP CPU spare
        "L15335-001",  # HP L-series spare
        "338-BJEU",  # Dell CPU spare
        "0A36527",  # Lenovo/IBM FRU
    ],
)
def test_oem_cpu_spares_are_not_reclassified(oem_spare):
    # OEM CPU spares ARE (probably) CPUs but unkeyable — they stay in `cpu` (out of scope),
    # and MUST NOT be mistaken for TE connectors by the numeric-dash rule. (Precision guard:
    # the TE 7-digit-core rule must reject the HP 6-digit-core+3-suffix shape.)
    assert classify_polluted_mpn(oem_spare) is None


def test_unrecognized_and_empty_return_none():
    assert classify_polluted_mpn("ZZQW9981XYZ") is None
    assert classify_polluted_mpn("") is None
    assert classify_polluted_mpn(None) is None


from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.commodity_registry import seed_commodity_schemas


def _cpu_card(db: Session, mpn: str) -> MaterialCard:
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn, category="cpu")
    card.category_source = "trio_source"
    card.category_tier = 95
    db.add(card)
    db.flush()
    return card


def test_cli_dry_run_changes_nothing_but_reports(db_session: Session):
    from app.management.fix_cpu_pollution import reclassify_cpu_pollution

    seed_commodity_schemas(db_session)
    te = _cpu_card(db_session, "5-1437720-3")
    db_session.commit()
    stats = reclassify_cpu_pollution(db_session, apply=False)
    db_session.refresh(te)
    assert stats["reclassified"] == 1
    assert stats["by_commodity"] == {"connectors": 1}
    assert te.category == "cpu"  # dry-run: unchanged


def test_cli_apply_reclassifies_pollution_only(db_session: Session):
    from app.management.fix_cpu_pollution import reclassify_cpu_pollution

    seed_commodity_schemas(db_session)
    te = _cpu_card(db_session, "5-1437720-3")  # TE connector
    cpu = _cpu_card(db_session, "SR3QS")  # real Intel CPU
    dram = MaterialCard(normalized_mpn="d1", display_mpn="5-9999999-9", category="dram")
    dram.category_source = "trio_source"
    dram.category_tier = 95
    db_session.add(dram)
    db_session.flush()
    db_session.commit()

    stats = reclassify_cpu_pollution(db_session, apply=True)
    for c in (te, cpu, dram):
        db_session.refresh(c)
    assert te.category == "connectors"
    assert te.category_source == "cpu_pollution_fix"
    assert te.category_tier == 96
    assert cpu.category == "cpu"  # real CPU untouched
    assert dram.category == "dram"  # non-cpu bucket untouched (CLI scopes to category='cpu')
    assert stats["reclassified"] == 1

"""Invariants for the commodity taxonomy tree (COMMODITY_TREE).

Guards the family split (Memory != Storage, Connectors != Electromechanical) and the
contract that every child key is a real, seeded, display-named commodity.
"""

from app.services.commodity_registry import (
    _DISPLAY_NAMES,
    COARSE_BUCKETS_WITHOUT_SEEDS,
    COMMODITY_SPEC_SEEDS,
    COMMODITY_TREE,
    get_all_commodities,
    get_parent_group,
)


def test_memory_and_storage_are_separate_groups():
    assert "Memory" in COMMODITY_TREE
    assert "Storage & Drives" in COMMODITY_TREE
    assert "Memory & Storage" not in COMMODITY_TREE
    assert set(COMMODITY_TREE["Memory"]) == {"dram", "flash"}
    assert set(COMMODITY_TREE["Storage & Drives"]) == {"ssd", "hdd", "tape_drives"}


def test_connectors_and_electromechanical_are_separate_groups():
    assert "Connectors, Interconnects & Cables" in COMMODITY_TREE
    assert "Electromechanical" in COMMODITY_TREE
    assert "Connectors & Electromechanical" not in COMMODITY_TREE
    assert set(COMMODITY_TREE["Connectors, Interconnects & Cables"]) == {"connectors", "cables", "sockets"}


def test_motors_moved_into_electromechanical():
    assert "motors" in COMMODITY_TREE["Electromechanical"]
    assert "motors" not in COMMODITY_TREE["Misc"]
    assert get_parent_group("motors") == "Electromechanical"


def test_no_child_key_duplicates():
    children = get_all_commodities()
    assert len(children) == len(set(children)), "duplicate child key in COMMODITY_TREE"


def test_every_child_key_has_display_name():
    for child in get_all_commodities():
        assert child in _DISPLAY_NAMES, f"{child} missing from _DISPLAY_NAMES"


def test_every_child_key_is_seeded():
    """Every tree child carries parametric seeds, except declared coarse buckets
    (ics_other, oem_assemblies) which intentionally have no honest parametric
    vocabulary."""
    for child in get_all_commodities():
        if child in COARSE_BUCKETS_WITHOUT_SEEDS:
            assert child not in COMMODITY_SPEC_SEEDS, f"{child} declared coarse but HAS seeds"
            continue
        assert child in COMMODITY_SPEC_SEEDS, f"{child} missing a seed block"


def test_group_count():
    assert len(COMMODITY_TREE) == 13

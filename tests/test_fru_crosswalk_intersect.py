"""Unit — intersect_decodes: the pure strict-intersection rule of the FRU crosswalk pass
(no DB).

Specs all approved substitutes share are FRU-level truths; anything they differ on (or
that any one of them omits) is never asserted.
"""

import pytest

from app.services.desc_extractor import DescResult
from app.services.fru_crosswalk_enrich import intersect_decodes
from app.services.mpn_decoder import DecodeResult


def _hdd(specs: dict) -> DecodeResult:
    return DecodeResult(commodity="hdd", vendor="seagate", specs=specs)


def test_full_agreement_keeps_all_specs():
    a = _hdd({"capacity_gb": 4000, "form_factor": '3.5"', "interface": "SAS"})
    b = _hdd({"capacity_gb": 4000, "form_factor": '3.5"', "interface": "SAS"})

    commodity, agreed, dropped = intersect_decodes([a, b])

    assert commodity == "hdd"
    assert agreed == {"capacity_gb": 4000, "form_factor": '3.5"', "interface": "SAS"}
    assert dropped == 0


def test_conflicting_value_dropped_and_counted_shared_keys_kept():
    a = _hdd({"capacity_gb": 4000, "rpm": 10000})
    b = _hdd({"capacity_gb": 4000, "rpm": 15000})

    commodity, agreed, dropped = intersect_decodes([a, b])

    assert commodity == "hdd"
    assert agreed == {"capacity_gb": 4000}  # rpm conflicts → never asserted
    assert dropped == 1


def test_key_present_in_only_one_decode_is_dropped_uncounted():
    # Absence is not agreement — the key is dropped, but it is NOT a value
    # conflict, so it does not count toward dropped_conflict.
    a = _hdd({"capacity_gb": 4000, "rpm": 7200})
    b = _hdd({"capacity_gb": 4000})

    commodity, agreed, dropped = intersect_decodes([a, b])

    assert commodity == "hdd"
    assert agreed == {"capacity_gb": 4000}
    assert dropped == 0


def test_commodity_disagreement_returns_none():
    # A FRU whose substitutes can't agree on what they ARE gets nothing asserted.
    a = _hdd({"capacity_gb": 4000})
    b = DecodeResult(commodity="ssd", vendor="samsung", specs={"capacity_gb": 3840})

    assert intersect_decodes([a, b]) == (None, {}, 0)


def test_single_decode_is_passthrough():
    a = _hdd({"capacity_gb": 8000, "form_factor": '3.5"'})

    commodity, agreed, dropped = intersect_decodes([a])

    assert commodity == "hdd"
    assert agreed == {"capacity_gb": 8000, "form_factor": '3.5"'}
    assert dropped == 0


def test_empty_list_raises():
    # No evidence is the CALLER's case to handle (it filters undecodable links
    # before calling) — raising here keeps a None commodity unambiguous: it always
    # means contradicting evidence, never missing evidence.
    with pytest.raises(ValueError):
        intersect_decodes([])


def test_three_way_intersection_requires_all_to_agree():
    # A key must be present in EVERY decode with an equal value; a key missing
    # from just one of three decodes is dropped even when the other two agree.
    a = _hdd({"capacity_gb": 4000, "form_factor": '3.5"', "rpm": 7200})
    b = _hdd({"capacity_gb": 4000, "form_factor": '3.5"', "rpm": 7200})
    c = _hdd({"capacity_gb": 4000, "rpm": 10000})

    commodity, agreed, dropped = intersect_decodes([a, b, c])

    assert commodity == "hdd"
    assert agreed == {"capacity_gb": 4000}  # form_factor absent from c; rpm conflicts
    assert dropped == 1  # only rpm is a counted value conflict


# ---------------------------------------------------------------------------
# DescResult inputs (the fru_desc_parse channel) — intersect_decodes is shared by
# both evidence channels, so the SAME contract must hold for desc extractions.
# ---------------------------------------------------------------------------


def _hdd_desc(specs: dict) -> "DescResult":
    return DescResult(commodity="hdd", specs=specs)


def test_desc_results_full_agreement_keeps_all_specs():
    # Extractions of "8TB 3.5 HDD 7.2K 12Gb/s SAS"-style qual-sheet prose.
    a = _hdd_desc({"capacity_gb": 8000, "rpm": "7200", "interface": "SAS"})
    b = _hdd_desc({"capacity_gb": 8000, "rpm": "7200", "interface": "SAS"})

    commodity, agreed, dropped = intersect_decodes([a, b])

    assert commodity == "hdd"
    assert agreed == {"capacity_gb": 8000, "rpm": "7200", "interface": "SAS"}
    assert dropped == 0


def test_desc_results_conflicting_value_dropped_and_counted():
    # An 8TB row next to an 18TB row: shared rpm/interface survive, capacity is
    # dropped AND counted.
    a = _hdd_desc({"capacity_gb": 8000, "rpm": "7200", "interface": "SAS"})
    b = _hdd_desc({"capacity_gb": 18000, "rpm": "7200", "interface": "SAS"})

    commodity, agreed, dropped = intersect_decodes([a, b])

    assert commodity == "hdd"
    assert agreed == {"rpm": "7200", "interface": "SAS"}
    assert dropped == 1


def test_desc_results_commodity_disagreement_returns_none():
    # HDD prose next to SSD prose — the linked rows can't agree on what the part IS.
    a = _hdd_desc({"capacity_gb": 450})
    b = DescResult(commodity="ssd", specs={"capacity_gb": 800})

    assert intersect_decodes([a, b]) == (None, {}, 0)


def test_single_desc_result_passes_all_its_specs():
    # One-of-N agreement: a lone extracting description asserts everything it parsed.
    a = _hdd_desc({"capacity_gb": 1200, "rpm": "10000", "interface": "SAS"})

    commodity, agreed, dropped = intersect_decodes([a])

    assert commodity == "hdd"
    assert agreed == {"capacity_gb": 1200, "rpm": "10000", "interface": "SAS"}
    assert dropped == 0

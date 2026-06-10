"""Unit — intersect_decodes: the pure D2 strict-intersection rule of the FRU crosswalk
pass (no DB).

Specs all approved substitutes share are FRU-level truths; anything they differ on (or
that any one of them omits) is never asserted.
"""

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


def test_empty_list_is_noop():
    assert intersect_decodes([]) == (None, {}, 0)


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

"""Accuracy guard for the storage MPN decoders — known part numbers → expected specs."""

import pytest

from app.services.mpn_decoder import decode_mpn

# (mpn, expected subset of specs, expected commodity)
CASES = [
    # Seagate modern family-coded
    ("ST4000NM0035", {"capacity_gb": 4000, "form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}, "hdd"),
    ("ST16000NM001G", {"capacity_gb": 16000, "form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}, "hdd"),
    ("ST1000DM010", {"capacity_gb": 1000, "form_factor": '3.5"', "usage_class": "Desktop / Client"}, "hdd"),
    ("ST500LM030", {"capacity_gb": 500, "form_factor": '2.5"', "usage_class": "Desktop / Client"}, "hdd"),
    ("ST8000VN004", {"capacity_gb": 8000, "form_factor": '3.5"', "usage_class": "NAS"}, "hdd"),
    # Western Digital modern (TB×10 capacity, family from suffix)
    ("WD40EFRX", {"capacity_gb": 4000, "form_factor": '3.5"', "usage_class": "NAS"}, "hdd"),
    ("WD20EZRZ", {"capacity_gb": 2000, "form_factor": '3.5"', "usage_class": "Desktop / Client"}, "hdd"),
    ("WD140EFGX", {"capacity_gb": 14000, "form_factor": '3.5"', "usage_class": "NAS"}, "hdd"),
    # Toshiba — MG enterprise 3.5" with explicit TB token; MQ 2.5" form only
    ("MG08ACA16TE", {"capacity_gb": 16000, "form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}, "hdd"),
    ("MQ01ABD100", {"form_factor": '2.5"'}, "hdd"),
    # HGST/Hitachi — prefix → form + usage (capacity not in a clean token here)
    ("HUH721212ALN604", {"form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}, "hdd"),
    ("HTS721010A9E630", {"form_factor": '2.5"'}, "hdd"),
    ("HMS5C4040ALE640", {"form_factor": '3.5"', "usage_class": "Desktop / Client"}, "hdd"),
]


@pytest.mark.parametrize("mpn,expected,commodity", CASES)
def test_storage_decode(mpn, expected, commodity):
    result = decode_mpn(mpn)
    assert result is not None, f"{mpn} did not decode"
    assert result.commodity == commodity
    for key, val in expected.items():
        assert result.specs.get(key) == val, f"{mpn}: {key} expected {val!r}, got {result.specs.get(key)!r}"


def test_old_seagate_scheme_not_misdecoded():
    # Old ST<ff><cap><rest> scheme must NOT match the modern gate (would misread capacity).
    assert decode_mpn("ST3500418AS") is None


def test_non_drive_mpn_returns_none():
    assert decode_mpn("LM358N") is None
    assert decode_mpn("GARBAGE123") is None
    assert decode_mpn("") is None
    assert decode_mpn(None) is None

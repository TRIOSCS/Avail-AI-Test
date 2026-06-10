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
    # Seagate modern unmapped families: the structured 0-led tail certifies the era,
    # so capacity still decodes even when the 2-letter family is not in the usage map.
    ("ST300MM0006", {"capacity_gb": 300}, "hdd"),
    ("ST1200MM0088", {"capacity_gb": 1200}, "hdd"),
    # Western Digital modern (TB×10 capacity, family from suffix)
    ("WD40EFRX", {"capacity_gb": 4000, "form_factor": '3.5"', "usage_class": "NAS"}, "hdd"),
    ("WD20EZRZ", {"capacity_gb": 2000, "form_factor": '3.5"', "usage_class": "Desktop / Client"}, "hdd"),
    ("WD140EFGX", {"capacity_gb": 14000, "form_factor": '3.5"', "usage_class": "NAS"}, "hdd"),
    # Western Digital LEGACY decimal-GB scheme (exactly-2-letter family code): digits/10 GB.
    # WD800BB / WD600BB are the audit's 1000×-error cards (3648, 622981) — 80 GB, not 80,000.
    ("WD800BB", {"capacity_gb": 80}, "hdd"),  # audit card 3648
    ("WD600BB", {"capacity_gb": 60}, "hdd"),  # audit card 622981
    ("WD2500JB", {"capacity_gb": 250}, "hdd"),
    ("WD360GD", {"capacity_gb": 36}, "hdd"),  # Raptor
    ("WD64AA", {"capacity_gb": 6.4}, "hdd"),  # very-old Caviar: implied decimal survives
    ("WD800BB-00JHC0", {"capacity_gb": 80}, "hdd"),  # dash revision after the 2-letter code
    # Toshiba — MG enterprise 3.5" with explicit TB token; MQ 2.5" form only
    ("MG08ACA16TE", {"capacity_gb": 16000, "form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}, "hdd"),
    ("MQ01ABD100", {"form_factor": '2.5"'}, "hdd"),
    # HGST/Hitachi — prefix → form + usage (capacity not in a clean token here).
    # HUS<digit> Ultrastar HDDs pin the positive side of the HUS(?=\d) lookahead — the
    # HUSMM/HUSSL SAS-SSD exclusion below must never regress into dropping these.
    ("HUH721212ALN604", {"form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}, "hdd"),
    ("HUS726T4TALA6L4", {"form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}, "hdd"),
    ("HUS156030VLS600", {"form_factor": '3.5"', "usage_class": "Enterprise / Datacenter"}, "hdd"),
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


@pytest.mark.parametrize(
    "mpn",
    [
        "ST39103FC",  # audit card 163617: 9.1 GB Cheetah 9LP — was misdecoded as 39,103 GB
        "ST373207",  # audit card 195043: 73 GB Cheetah 10K.7 — was misdecoded as 373,207 GB
        "ST373207LC",  # same drive, SCSI 80-pin suffix form
        "ST373455LW",  # audit card 413156's evidence chain (same naive digit pathology)
        "ST973402SS",  # legacy 2.5" SAS shape (digits 973402 are NOT a capacity)
        "ST173404LW",  # ST1 half-height legacy shape
    ],
)
def test_legacy_seagate_shapes_return_none(mpn):
    # The legacy ST<ff-digit><digits><iface letters> grammar mixes a form-factor digit
    # with MB digits, and pre-~1996 models encode UNFORMATTED MB — no pattern-only
    # grammar can split the eras with certainty, so these must return None rather than
    # ever emitting the raw digit string as GB (audit failure class 1).
    assert decode_mpn(mpn) is None, f"{mpn} must not decode (legacy Seagate shape)"


@pytest.mark.parametrize(
    "mpn",
    [
        "ST232BDR",  # audit card 674852: STMicro RS-232 transceiver — was a "232 GB drive"
        "ST3232EBDR",  # STMicro ST3232E, SO + reel
        "ST485",  # STMicro RS-485 transceiver, bare order code
        "STM32F407VGT6",  # STM32 MCU
        "STM8S003F3P6",  # STM8 MCU
    ],
)
def test_stmicro_order_codes_never_pass_the_seagate_gate(mpn):
    # ST-prefix collision (audit failure class 2): STMicroelectronics order codes end in
    # package/reel letters, never the modern Seagate 0-led structured tail — both the
    # strengthened accept gate and the explicit deny-shape must reject them.
    assert decode_mpn(mpn) is None, f"{mpn} is an STMicro part, not a Seagate drive"


@pytest.mark.parametrize("mpn", ["WD800AAJS", "WD740ADFD", "WD5000AAKX", "WD1002FAEX"])
def test_wd_ambiguous_era_shapes_return_none(mpn):
    # 3-digit + 4-letter WD shapes without a recognized modern family token are ambiguous
    # between the legacy decimal-GB era (WD800AAJS = 80 GB) and the TB era (WD140EFGX =
    # 14 TB); the 4-digit + 4-letter scheme even mixes units (WD5000AAKX = 500 GB,
    # WD1002FAEX = 1 TB). None of these may emit a capacity.
    assert decode_mpn(mpn) is None, f"{mpn} is era-ambiguous — must not decode"


def test_non_drive_mpn_returns_none():
    assert decode_mpn("LM358N") is None
    assert decode_mpn("GARBAGE123") is None
    assert decode_mpn("") is None
    assert decode_mpn(None) is None


@pytest.mark.parametrize("mpn", ["MGK50", "MGJN9", "DT10171-H7R6-4F", "MDR60"])
def test_short_oem_spare_not_misdecoded_as_toshiba(mpn):
    # Dell/OEM spare numbers share Toshiba's 2-char prefix but not the family structure
    # (prefix + 2 digits + 3-letter code). The tightened gate must reject them, NOT emit a
    # bogus 3.5"/Enterprise drive. Regression for the dry-run's MGK50/MGJN9/DT10171 hits.
    assert decode_mpn(mpn) is None, f"{mpn} should not decode as a Toshiba drive"


@pytest.mark.parametrize("mpn", ["HUSMM1640ASS204", "HUSSL4010BSS600", "HUSMR1650ASS204"])
def test_hgst_sas_ssd_families_not_misdecoded_as_hdd(mpn):
    # HUSMM/HUSSL/HUSMR are Ultrastar SAS *SSDs* (2.5"), not Ultrastar HDDs — the HUS gate
    # requires a digit next (HUS72…, HUS156…) so these return None instead of a wrong
    # 3.5"/Enterprise HDD decode.
    assert decode_mpn(mpn) is None, f"{mpn} must not decode as an HDD"


def test_wd_mobile_drive_capacity_only_no_guessed_form_factor():
    # WD10JPLX is a 2.5" mobile drive whose suffix does not start "S"; the old rule mislabeled
    # it 3.5". Capacity is reliable (WD10 = 1 TB); form_factor must be ABSENT, not wrong.
    result = decode_mpn("WD10JPLX")
    assert result is not None
    assert result.specs.get("capacity_gb") == 1000
    assert "form_factor" not in result.specs

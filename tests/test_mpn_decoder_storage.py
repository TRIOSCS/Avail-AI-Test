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
    # Western Digital modern (revision-digit scheme: leading digits = TB, final digit =
    # revision marker; family from suffix)
    ("WD40EFRX", {"capacity_gb": 4000, "form_factor": '3.5"', "usage_class": "NAS"}, "hdd"),
    ("WD20EZRZ", {"capacity_gb": 2000, "form_factor": '3.5"', "usage_class": "Desktop / Client"}, "hdd"),
    ("WD140EFGX", {"capacity_gb": 14000, "form_factor": '3.5"', "usage_class": "NAS"}, "hdd"),
    # Re-audit 2026-06-10 pins (residual class 1): the final digit is a REVISION marker —
    # the round-1 TB×10 read minted 10.1/12.1/4.2/2.2 TB ghosts for these exact cards.
    ("WD101EFBX", {"capacity_gb": 10000, "form_factor": '3.5"', "usage_class": "NAS"}, "hdd"),  # card 578746
    ("WD100EFAX", {"capacity_gb": 10000, "form_factor": '3.5"', "usage_class": "NAS"}, "hdd"),  # rev-0 sibling
    ("WD121PURP", {"capacity_gb": 12000, "form_factor": '3.5"', "usage_class": "Surveillance"}, "hdd"),  # card 576065
    ("WD42PURZ", {"capacity_gb": 4000, "form_factor": '3.5"', "usage_class": "Surveillance"}, "hdd"),  # card 576143
    ("WD40PURZ", {"capacity_gb": 4000, "form_factor": '3.5"', "usage_class": "Surveillance"}, "hdd"),  # rev-0 sibling
    ("WD22LMPT1", {"capacity_gb": 2000}, "hdd"),  # card 94561 — no known family token ⇒ capacity only
    # Shipped fractional-TB exception: Caviar-Green-era 1.5/2.5 TB points really shipped —
    # the revision-digit rule must NOT flatten them to 1/2 TB.
    ("WD15EADS", {"capacity_gb": 1500}, "hdd"),
    ("WD25EZRS", {"capacity_gb": 2500, "form_factor": '3.5"', "usage_class": "Desktop / Client"}, "hdd"),
    # The REAL 1.2 TB MM-series part — its digit-dropped truncation pins None below.
    ("ST1200MM0198", {"capacity_gb": 1200}, "hdd"),
    # The dual-brand W4 headline part (Enterprise Performance 15K, MP family) — its
    # decode feeds tests across spec_tiers/backfill, so the MP envelope must hold.
    ("ST300MP0016", {"capacity_gb": 300}, "hdd"),
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


# ── Re-audit 2026-06-10 (round 2) ────────────────────────────────────────


@pytest.mark.parametrize(
    "mpn",
    [
        "ST120MM0198",  # re-audit card 120169: digit-dropped truncation of the 1.2 TB ST1200MM0198
        "ST200NM0055",  # same class: truncation of the 2 TB ST2000NM0055 (NM floor is 500 GB)
        "ST30000MM0006",  # envelope ceiling: no 30 TB 2.5" SAS MM drive exists (max 2.4 TB)
    ],
)
def test_out_of_envelope_seagate_shapes_return_none(mpn):
    # Residual class 2: a truncated/malformed string can pass the structured-tail SHAPE
    # gate, so the decoded capacity must also sit inside the family's shipped envelope
    # (_SEAGATE_ENVELOPE). Out-of-envelope ⇒ NO decode at all — never a best-effort
    # capacity (and never the form/usage of a string we distrust).
    assert decode_mpn(mpn) is None, f"{mpn} must not decode (out of family envelope)"


def test_unknown_seagate_family_returns_none():
    # A modern-shaped string whose 2-letter family has no vetted envelope cannot be
    # range-checked — emitting its capacity would be a best-effort guess.
    assert decode_mpn("ST4000ZZ0011") is None
    # The closed family table also excludes Seagate's modern-shaped SAS SSD lines
    # (Nytro FM) — an hdd decode for an SSD would be wrong twice over.
    assert decode_mpn("ST400FM0233") is None


def test_six_digit_seagate_capacity_group_never_matches():
    # Strict digit-count validation: a 6-digit capacity group would read ≥100 TB —
    # always a malformed string, structurally excluded by the \d{3,5} gate.
    assert decode_mpn("ST120000NM0011") is None


def test_every_mapped_seagate_family_has_an_envelope():
    # _seagate refuses families without an envelope, so every form/usage-mapped family
    # MUST have one — otherwise the map entry is dead code and real parts stop decoding.
    from app.services.mpn_decoder.storage import _SEAGATE_ENVELOPE, _SEAGATE_FAMILY

    assert set(_SEAGATE_FAMILY) <= set(_SEAGATE_ENVELOPE)


def test_shipped_capacity_grid_boundaries():
    # The discrete shipped-capacity vocabulary (residual classes 1+2 backstop): real
    # grid points pass, the re-audit's four ghost points (1-5% off — invisible to any
    # magnitude ceiling) sit OFF the grid.
    from app.services.mpn_decoder.storage import HDD_SHIPPED_CAPACITY_GB

    assert 10000 in HDD_SHIPPED_CAPACITY_GB
    assert 10100 not in HDD_SHIPPED_CAPACITY_GB  # WD101EFBX ghost (10.1 TB)
    assert 12100 not in HDD_SHIPPED_CAPACITY_GB  # WD121PURP ghost (12.1 TB)
    assert 4200 not in HDD_SHIPPED_CAPACITY_GB  # WD42PURZ ghost (4.2 TB)
    assert 2200 not in HDD_SHIPPED_CAPACITY_GB  # WD22… ghost (2.2 TB)
    # Legacy decimal-GB and fractional-TB points the round-1 pins rely on stay on-grid.
    assert {6.4, 36, 60, 80, 250, 1500, 2500} <= HDD_SHIPPED_CAPACITY_GB


def test_off_grid_capacity_is_dropped_to_the_dropped_channel():
    # No 17 TB HDD has ever shipped (16 and 18 exist): the T-token read passes Toshiba's
    # shape gate, so the grid backstop must catch it — capacity moves to result.dropped
    # (writer.py WARNs on it), the trustworthy prefix-derived specs still decode.
    result = decode_mpn("MG09ACA17TE")
    assert result is not None
    assert "capacity_gb" not in result.specs
    assert result.dropped == {"capacity_gb": 17000}
    assert result.specs["form_factor"] == '3.5"'
    assert result.specs["usage_class"] == "Enterprise / Datacenter"


def test_grid_emptied_decode_returns_none():
    # When the off-grid capacity was the decode's ONLY spec (legacy WD emits capacity
    # only), dropping it empties the decode — that is no decode at all.
    assert decode_mpn("WD555AB") is None  # 55.5 GB was never a shipped point


def test_on_grid_decodes_keep_an_empty_dropped_channel():
    result = decode_mpn("ST4000NM0035")
    assert result is not None
    assert result.dropped == {}

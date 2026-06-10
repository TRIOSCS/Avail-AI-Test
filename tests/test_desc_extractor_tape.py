"""Accuracy guard for the tape-drive description extractor — REAL corpus strings → exact
specs.

Every description below is verbatim from TRIO's part master
(/root/source_ingest/LSC1__Material__c.csv, Material_Description__c). Expectations are
FULL equality — a new key appearing unexpectedly is as much a failure as a missing one.
"""

import pytest

from app.services.desc_extractor import extract_desc

# (real description, commodity_hint or None, exact expected specs)
CASES = [
    # ── TRIO part-master "<Label>, …" grammar ────────────────────────────
    (
        "Tape Drive, 400/800gb Ultrium Lto-3 HH SCSI LVD External",  # 400/800gb pair never read
        None,
        {"drive_type": "LTO-3", "interface": "SCSI", "form_factor": "Half-Height"},
    ),
    (
        "Tape Drive, Jag 5,TS3500, TS1150, 3592-E08",  # three agreeing Jaguar-gen-5 signals
        None,
        {"drive_type": "TS1150"},
    ),
    ("Tape, JAG 6, SAS", None, {"drive_type": "TS1160", "interface": "SAS"}),
    ("Tape, JAG 7", None, {"drive_type": "TS1170"}),
    (
        "Tape LTO-9, FH, SAS",  # comma-less first token "Tape" routes (digit blocks the lead)
        None,
        {"drive_type": "LTO-9", "interface": "SAS", "form_factor": "Full-Height"},
    ),
    (
        "Tape Drive, IBM 2.5 / 6.25TB Ultrium 6 LTO6 half-high SAS tape drive, 7226-1U3, G6HxServer, SAS",
        None,
        {"drive_type": "LTO-6", "interface": "SAS", "form_factor": "Half-Height"},
    ),
    # ── body-token routing (no lead label) ───────────────────────────────
    ("LTO9 CANIS", None, {"drive_type": "LTO-9"}),  # glued generation token
    (
        "TS4300 LTO7 HH Fibre Channel Drive",  # TS4300 is a library model — matches nothing
        None,
        {"drive_type": "LTO-7", "interface": "FC", "form_factor": "Half-Height"},
    ),
    (
        "DAT160 INTERNAL USB TAPE DRIVE 80/160GB MFG REF",
        None,
        {"drive_type": "DAT", "interface": "USB"},
    ),
    (
        "TS1160 Tape drive with caddy for TS4500, 20-40TB SAS 12Gb",
        None,
        {"drive_type": "TS1160", "interface": "SAS"},
    ),
    (
        "ASSY,DR,BAY,FC,LTO8,TL2/4K",  # neutral "ASSY," lead — body LTO8 rescues the route
        None,
        {"drive_type": "LTO-8", "interface": "FC"},
    ),
    ("FIBRE LTO5(ROHS)", None, {"drive_type": "LTO-5", "interface": "FC"}),
    (
        "EJ014B Black 3TB 1U Rack mount SAS 6Gb/s Interface LTO-5 Ultrium 3000 Tape Drive",
        None,
        {"drive_type": "LTO-5", "interface": "SAS"},  # "Ultrium 3000" rejected; no capacity key
    ),
    (
        "IBM 3588-F8C TS1080 LTO 8 FH FC for TS4500",  # TS1080/TS4500 match nothing — LTO-8
        None,
        {"drive_type": "LTO-8", "interface": "FC", "form_factor": "Full-Height"},
    ),
    # ── hint-routed grammar without a routing token ──────────────────────
    ("LTO GEN5", "tape_drives", {"drive_type": "LTO-5"}),
    ("LTO7HHSAS", "tape_drives", {}),  # fully glued — every boundary dead, deliberate miss
    # ── media/supplies rows: cartridges and label packs are NOT drives ───
    ("Tape, Cleaning Cartridge, DAT 320, IBM", None, {}),
    ("HP Q2078A LTO8 30TB RW DATA CARTRIDGE", None, {}),
    ("SPS-Data Cartridge, LTO-8 30TB WORM", None, {}),
    ("LTO6-LABEL", None, {}),  # barcode-label pack — glued LTO6 routes, _MEDIA suppresses
]


@pytest.mark.parametrize("description,hint,expected", CASES)
def test_tape_extract_exact(description, hint, expected):
    result = extract_desc(description, commodity_hint=hint)
    assert result is not None, f"{description!r} did not extract"
    assert result.commodity == "tape_drives"
    assert result.specs == expected
    assert result.confidence == 0.90


def test_library_lead_is_foreign_even_with_drive_grammar():
    # A library is not a drive: the "Library," lead suppresses extraction entirely —
    # accepted conservative loss, even though Jag6/3592 drive tokens appear.
    assert extract_desc("Library, 3592 Tape Drive, Jag6 Drive") is None


def test_conflicting_generations_and_interfaces_omit_both_keys():
    # Conflict pins for the unique-survivor contract: LTO-5×LTO-6 drops drive_type
    # and SAS×FC drops interface — never first-match/max picked.
    result = extract_desc("Tape Drive, LTO-5 / LTO-6, SAS FC")
    assert result is not None
    assert result.specs == {}

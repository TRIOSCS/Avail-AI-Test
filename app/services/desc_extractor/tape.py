"""Deterministic tape-drive description→spec extraction (TRIO inventory grammar).

What: reads drive type / interface / form factor out of compact human tape-drive
      descriptions like ``Tape Drive, 400/800gb Ultrium Lto-3 HH SCSI LVD
      External`` or ``Tape, JAG 7`` — NO network, NO LLM. Every emitted value is
      a seeded tape_drives enum member per app/data/commodity_seeds.json;
      record_spec independently re-validates enum members and skips unseeded
      keys. The drift guard in tests/test_desc_extractor_routing.py pins the
      vocabularies against the seeds.
Called by: app/services/desc_extractor/__init__.py (extract_desc routing).
Depends on: _common (SpecDict alias only) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- Media/supplies rows emit NOTHING: a CARTRIDGE / CLEANING / MEDIA / LABEL / WORM
  token means the row is a cartridge, cleaning kit or barcode-label pack — not a
  drive — even though the "Tape," lead or a glued LTO body token routes it here
  ("Tape, Cleaning Cartridge, DAT 320", "LTO6-LABEL"). Same conservative-loss
  rationale as the ``Library,`` exclusion below.
- drive_type collects ALL grammar hits (LTO-N / LTO GEN N / Ultrium N, the closed
  TS11xx + 3592-model + Jaguar maps, DAT/DDS, AIT) and emits only a unique
  surviving member. "Ultrium 3000" (a product line, not a generation) is rejected
  by ``(?!\\d)``; TS1080/TS4500 (libraries) match nothing.
- interface vocab is {SAS, FC, SCSI, USB}; unique-or-omit ("SAS/FC" combos drop).
- form_factor from HH/FH and the spelled half-/full-height forms; the glued
  "LTO7HHSAS" grammar is a deliberate miss (no word boundary).
- NOT extracted: native_capacity_gb ("400/800gb", "2.5 / 6.25TB" are native/
  compressed pairs — drive_type already implies capacity) and encryption. The
  ``Library,`` lead stays unrouted upstream (a library is not a drive).
"""

import re

from app.services.desc_extractor._common import SpecDict

# Canonical tape_drives enum strings — MUST match the tape_drives entry in
# app/data/commodity_seeds.json (drift-guarded).
DAT, AIT = "DAT", "AIT"
FULL_HEIGHT, HALF_HEIGHT = "Full-Height", "Half-Height"

# Media/supplies tokens: a cartridge, cleaning cartridge, loose media or barcode
# label pack is NOT a drive — extraction is suppressed entirely (a mis-bucketed
# tape_drives card would otherwise take a 0.90 drive_type that outranks the AI
# reader). Mirrors the upstream "Library," foreign-lead rationale.
_MEDIA = re.compile(r"\bCARTRIDGES?\b|\bCLEANING\b|\bMEDIA\b|\bLABELS?\b|\bWORM\b")

_LTO = re.compile(r"\bLTO[- ]?([3-9])\b")
_LTO_GEN = re.compile(r"\bLTO\s?GEN\s?([3-9])\b")
_ULTRIUM = re.compile(r"\bULTRIUM\s?([3-9])(?!\d)\b")
_TS11 = re.compile(r"\bTS11(40|50|55|60|70)\b")
_3592 = re.compile(r"\b3592[- ]?(E07|EH7|E08|EH8|55F|55E|60F|60E)\b")
_3592_BY_MODEL = {
    "E07": "TS1140",
    "EH7": "TS1140",
    "E08": "TS1150",
    "EH8": "TS1150",
    "55F": "TS1155",
    "55E": "TS1155",
    "60F": "TS1160",
    "60E": "TS1160",
}
_JAG = re.compile(r"\bJAG\s?([4-7])(A?)\b")
_JAG_BY_GEN = {"4": "TS1140", "5": "TS1150", "5A": "TS1155", "6": "TS1160", "7": "TS1170"}
_DAT = re.compile(r"\bDAT\d{0,3}\b|\bDDS\b")
_AIT = re.compile(r"\bAIT\b")

_IFACE_PATTERNS = (
    ("SAS", re.compile(r"\bSAS\b")),
    ("FC", re.compile(r"\bFC\b|\bFIBRE(?: CHANNEL)?\b|\bFIBER CHANNEL\b")),
    ("SCSI", re.compile(r"\bSCSI\b")),
    ("USB", re.compile(r"\bUSB\b")),
)
_FORM_PATTERNS = (
    (HALF_HEIGHT, re.compile(r"\bHH\b|\bHALF[- ]HIGH\b|\bHALF[- ]HEIGHT\b")),
    (FULL_HEIGHT, re.compile(r"\bFH\b|\bFULL[- ]HEIGHT\b|\bFULL[- ]HIGH\b")),
)


def _drive_type(text: str) -> str | None:
    """Distinct surviving seeded drive_type member, or None (absent / conflict)."""
    members = {f"LTO-{m.group(1)}" for m in _LTO.finditer(text)}
    members |= {f"LTO-{m.group(1)}" for m in _LTO_GEN.finditer(text)}
    members |= {f"LTO-{m.group(1)}" for m in _ULTRIUM.finditer(text)}
    members |= {f"TS11{m.group(1)}" for m in _TS11.finditer(text)}
    members |= {_3592_BY_MODEL[m.group(1)] for m in _3592.finditer(text)}
    for m in _JAG.finditer(text):
        member = _JAG_BY_GEN.get(m.group(1) + m.group(2))
        if member:  # unmapped Jaguar revisions (e.g. "JAG 4A") are dropped, never guessed
            members.add(member)
    if _DAT.search(text):
        members.add(DAT)
    if _AIT.search(text):
        members.add(AIT)
    return members.pop() if len(members) == 1 else None


def _interface(text: str) -> str | None:
    hits = {name for name, pattern in _IFACE_PATTERNS if pattern.search(text)}
    return hits.pop() if len(hits) == 1 else None


def _form_factor(text: str) -> str | None:
    hits = {name for name, pattern in _FORM_PATTERNS if pattern.search(text)}
    return hits.pop() if len(hits) == 1 else None


def extract_tape(text: str) -> SpecDict:
    """Extract tape_drives specs from an upper-cased, whitespace-collapsed
    description."""
    if _MEDIA.search(text):
        return {}  # cartridge / cleaning / label / WORM media row — not a drive
    specs: SpecDict = {}
    drive_type = _drive_type(text)
    if drive_type is not None:
        specs["drive_type"] = drive_type
    interface = _interface(text)
    if interface is not None:
        specs["interface"] = interface
    form_factor = _form_factor(text)
    if form_factor is not None:
        specs["form_factor"] = form_factor
    return specs

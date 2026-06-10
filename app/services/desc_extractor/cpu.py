"""Deterministic CPU description→spec extraction (SFDC/TRIO + HP spares grammars).

What: reads CPU facets out of compact human processor descriptions like
      ``IC,uP,CFL,i5-8400,2.8GHz,65W,9MB``, ``SPS-CPU BDW E5-2650L V4 14C 1_7GHZ
      65W`` or ``Xeon GOLD 6134 3.2G 8C 130W`` — NO network, NO LLM. Every
      emitted value is a seeded cpu enum member / in-range numeric per
      app/data/commodity_seeds.json; record_spec independently re-validates enum
      members and skips unseeded keys, but numeric ranges are enforced ONLY here
      — the drift guard in tests/test_desc_extractor_routing.py pins enums,
      ranges AND the whole model-spec table against the seeds.
Called by: app/services/desc_extractor/__init__.py (extract_desc routing — which
      also calls is_cpu_pollution, the step-0 deny-list from
      source_ingest/analysis/CPU_DECODE_FEASIBILITY.md).
Depends on: _common (SpecDict alias + unique_or_none), app/data/
      cpu_model_specs.json (curated ARK-style model→spec table).

Grammars (all corpus-verified in CPU_DECODE_FEASIBILITY.md §2):
- HP board-IC: ``IC,uP,<codename>,<model>,<GHz>,<W>,<cache>`` (comma or space
  delimited — routing gates on the full ``IC,uP`` prefix; bare ``IC,`` stays the
  general components bin).
- HP spares: ``SPS-CPU/SPS-PROC <codename> <model> <vN> <nC> <GHz> <W>`` — incl.
  underscore decimals (``1_7GHZ``), ``Gz`` misspellings, glued ``E52650Lv2``.
- Generic model strings: Xeon E3/E5/E7-NNNN vN, Scalable GOLD/SILVER/PLATINUM/
  BRONZE NNNN (+ HP ``Xeon-G/-S/-P/-B`` letter forms), Core i3/i5/i7/i9-NNNN,
  EPYC NNNN, Ryzen N NNNN.
- Spec tokens: core count (``14C`` glued / ``8-Core`` / ``12 CORE`` / ``12CORE``),
  GHz (``2.8GHz`` / ``2.50 GHz`` / ``1_7GHZ`` / ``2.3Gz`` / bare ``3.2G`` with a
  mandatory decimal), TDP ``NNNW`` — emitted as ``tdp_watts``, NEVER ``wattage``
  (the wattage key stays structurally psu-only).
- Architecture: HP codename map (CFL/KBL/BDW/SKL/HSW/CLX/ICL + corpus-verified
  SNB; ``INTCFL-R``/``SKL-SP`` composites accepted) → full Intel architecture
  names → E-series vN suffix map → model→spec table. AMD architecture comes
  ONLY from the table (corpus rows mislabel Zen generations in prose).

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- unique-or-omit on every token class; turbo clocks (``(3.2GHz Turbo)``,
  ``up to 4.40 GHz``) are dropped, so a base+turbo pair emits the base.
- bare numeric tokens (core count / TDP) require an explicit CPU-context signal
  (_CPU_CONTEXT) — hint-routed MPN-echo strings like ``812H-1C-CEF12VDC`` or
  ``EPS4-24W`` emit nothing.
- The model→spec table is merged UNDER directly-extracted tokens — a
  desc-stated GHz/core-count always beats the table. Lookup requires exactly
  ONE distinct extracted model string.
- family is emitted only from explicit family signals (XEON word / model-string
  class) and only for seeded members; Pentium/Celeron/Itanium/Opteron rows emit
  spec tokens but no family.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from app.services.desc_extractor._common import SpecDict, unique_or_none

# Canonical family enum strings — MUST match the cpu entry in
# app/data/commodity_seeds.json (drift-guarded).
XEON = "Xeon"
CORE_I = "Core i-series"
EPYC = "EPYC"
RYZEN = "Ryzen"
THREADRIPPER = "Threadripper"
ATOM = "Atom"

# Canonical architecture enum strings (drift-guarded against the seeds).
# HP codename map per CPU_DECODE_FEASIBILITY.md §2/§5 (+ SNB, corpus-verified in
# "SPS-Proc SNB E5-4650L 8C 2.6GHz 20M 115W").
_CODENAME_ARCH = {
    "CFL": "Coffee Lake",
    "KBL": "Kaby Lake",
    "BDW": "Broadwell",
    "SKL": "Skylake",
    "HSW": "Haswell",
    "CLX": "Cascade Lake",
    "ICL": "Ice Lake",
    "SNB": "Sandy Bridge",
}
# Composite forms: "INTCFL-R" (SPS-CPU INTCFL-R i7-9700K), "SKL-SP"/"SKL-W",
# "CFL-R" — optional INT prefix, optional -suffix of 1-3 letters.
_CODENAME = re.compile(r"\b(?:INT)?(CFL|KBL|BDW|SKL|HSW|CLX|ICL|SNB)(?:-[A-Z]{1,3})?\b")
# Full Intel architecture names ("GOLD 6126 SKYLAKE CPU 2.6GHZ…"). AMD names
# (ZEN…) are deliberately absent — corpus prose mislabels Zen generations.
_ARCH_NAMES = {
    "COFFEE LAKE": "Coffee Lake",
    "KABY LAKE": "Kaby Lake",
    "BROADWELL": "Broadwell",
    "SKYLAKE": "Skylake",
    "HASWELL": "Haswell",
    "CASCADE LAKE": "Cascade Lake",
    "ICE LAKE": "Ice Lake",
    "SANDY BRIDGE": "Sandy Bridge",
    "IVY BRIDGE": "Ivy Bridge",
}
_ARCH_NAME = re.compile(r"\b(" + "|".join(sorted(_ARCH_NAMES, key=len, reverse=True)) + r")\b")
# Xeon E3/E5/E7 generation-suffix map (v2=Ivy…v6=Kaby holds across all three).
_VN_ARCH = {"V2": "Ivy Bridge", "V3": "Haswell", "V4": "Broadwell", "V5": "Skylake", "V6": "Kaby Lake"}

# ── model-string grammars ────────────────────────────────────────────────
# Xeon E3/E5/E7: hyphen optional (glued "E52650Lv2"), suffix letters must not
# precede a digit (so "E5-2673V4" keeps V4 as the generation, not a suffix),
# vN optionally space-separated ("E5-2650L V4").
_XEON_E = re.compile(r"\bE([357])-?(\d{4})((?:[A-Z](?!\d)){0,2}) ?(?:V(\d))?\b")
# Scalable: model numbers start 3-9 (kills "PLATINUM 1100W" PSU-grade shapes —
# 80-PLUS grades never carry a 3xxx-9xxx model), suffix letter excludes W (a
# trailing W is a wattage, never a Scalable suffix).
_XEON_SCALABLE = re.compile(r"\b(GOLD|SILVER|PLATINUM|BRONZE)[ -]?([3-9]\d{3}[A-VX-Z]?)\b")
# HP letter form: "SPS-CPU SKL Xeon-G 6138 20c 2.0G 125W".
_XEON_SCALABLE_HP = re.compile(r"\bXEON-([GSPB]) ?([3-9]\d{3}[A-VX-Z]?)\b")
_HP_SCALABLE_LETTER = {"G": "GOLD", "S": "SILVER", "P": "PLATINUM", "B": "BRONZE"}
# Core iN: 4-5 digit model + suffix letters that may carry a digit ("I5-1035G7").
_CORE_I_MODEL = re.compile(r"\bI([3579])-(\d{4,5})((?:[A-Z]\d?){0,2})\b")
_CORE_I_WORD = re.compile(r"\bCORE ?I[3579]\b")
_EPYC_MODEL = re.compile(r"\bEPYC ?(\d{4}[A-Z]?|7H12)\b")
_EPYC_WORD = re.compile(r"\bEPYC\b")
_RYZEN_MODEL = re.compile(r"\bRYZEN ?([3579]) ?(?:PRO )?(\d{4}[A-Z]{0,2})\b")
_RYZEN_WORD = re.compile(r"\bRYZEN\b")
_XEON_WORD = re.compile(r"\bXEON\b")
_THREADRIPPER_WORD = re.compile(r"\bTHREADRIPPER\b")
_ATOM_WORD = re.compile(r"\bATOM\b")

# CPU-context gate for the BARE numeric tokens (core count / TDP): on a
# hint-routed cpu card whose "description" is an MPN echo, glued shapes like
# "812H-1C-CEF12VDC" or "EPS4-24W" would otherwise read as core_count/tdp_watts.
# Mirrors gpu.py's memory_gb context rule: cores/TDP emit only when the text
# carries an explicit CPU signal (commodity word, uP lead token, family word,
# model string, codename/architecture token, or a GHz clock).
_CPU_CONTEXT = re.compile(
    r"\bCPU\b|\bPROCESSORS?\b|\bPROC\b|\bUP\b"
    r"|\bXEON\b|\bEPYC\b|\bRYZEN\b|\bTHREADRIPPER\b|\bATOM\b"
    r"|\bITANIUM ?2?\b|\bOPTERON\b|\bPENTIUM\b|\bCELERON\b"
)

# ── spec-token grammars ──────────────────────────────────────────────────
# GHz: "2.8GHZ" / "2.50 GHZ" / corpus "2.3Gz"; underscore decimals "1_7GHZ";
# bare-G REQUIRES a decimal ("3.2G" yes, "10G" link speeds no — and "9.6GT/S"
# fails the trailing boundary).
_GHZ = re.compile(r"\b(\d(?:\.\d{1,2})?) ?(?:GHZ|GZ)\b")
_GHZ_UNDERSCORE = re.compile(r"\b(\d)_(\d{1,2}) ?GHZ\b")
_GHZ_BARE_G = re.compile(r"\b(\d\.\d{1,2})G\b")
# Core count: glued "14C" (word-bounded, so "I2C"/"53C4" never match) or
# "8-Core"/"12 CORE"/"12CORE".
_CORES_GLUED = re.compile(r"\b(\d{1,3})C\b")
_CORES_WORD = re.compile(r"\b(\d{1,3})[- ]?CORES?\b")
# TDP: explicit W/WATT(S) unit, 2-3 digits ("1/4W" resistor fractions and glued
# "150W22C" compounds never match). Emitted as tdp_watts ONLY — the wattage key
# exists solely on the power_supplies route.
_TDP = re.compile(r"\b(\d{2,3}) ?(?:W|WATTS?)\b")

# Seeded cpu numeric_ranges — the only range gates (record_spec performs no
# numeric_range check); pinned against the seeds by the drift guard.
_CORE_MIN, _CORE_MAX = 1, 256
_GHZ_MIN, _GHZ_MAX = 0.5, 6.0
_TDP_MIN, _TDP_MAX = 10, 500

# ── step-0 pollution deny-list (CPU_DECODE_FEASIBILITY.md §0/§6) ─────────
# The SFDC CPU bucket is polluted with passives/connectors/tape parts whose
# MPN-echo descriptions must never reach the cpu grammars. Patterns are the
# empirically-found false-positive classes from the report — anchored MPN
# shapes plus the tape-library context words that broke s-spec matching.
_POLLUTION = (
    re.compile(r"^GRM\d"),  # Murata GRM MLCC ("GRM155R71C104MA88D")
    re.compile(r"^EE[EU]"),  # Panasonic EEEF/EEUF caps ("EEEFK1E471GP")
    re.compile(r"^B72\d"),  # EPCOS/TDK varistors ("B72220P3271K102")
    re.compile(r"^\d{5}[A-Z]\d{3}[A-Z]AT"),  # AVX MLCC ("06035A101JAT2A")
    re.compile(r"^SN74"),  # TI logic ("SN74ALVC244PWR")
    re.compile(r"^SMAJ"),  # TVS diodes ("SMAJ24CA-13-F")
    re.compile(r"^\d?-?\d{6}-\d$"),  # TE connectors ("640456-9", "1-640456-0")
    re.compile(r"\bSL500\b|\bSL85Z\b|\bSTK\b|\bLTO-?\d?\b"),  # StorageTek/tape-library contexts
)

# HTML blobs appear in real corpus descriptions ("<h2><b>Intel Xeon Gold 6148…").
# Strips complete tags AND a truncated trailing tag (corpus rows are cut mid-tag).
_HTML_TAG = re.compile(r"<[^>]*>|<[^>]*$")

_SPECS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cpu_model_specs.json"
_TABLE_KEYS = ("family", "socket", "core_count", "clock_speed_ghz", "tdp_watts", "architecture")


@lru_cache(maxsize=1)
def load_model_specs() -> dict[str, dict]:
    """The curated model→spec table from app/data/cpu_model_specs.json."""
    with open(_SPECS_PATH) as f:
        models: dict[str, dict] = json.load(f)["models"]
    return models


def _clean(text: str) -> str:
    """Strip HTML tags and re-collapse whitespace (input is already uppercased)."""
    return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", text)).strip()


def is_cpu_pollution(text: str) -> bool:
    """True when the description matches a known non-CPU pollution shape."""
    cleaned = _clean(text)
    return any(p.search(cleaned) for p in _POLLUTION)


def _models(text: str) -> set[str]:
    """All normalized model strings found (any class)."""
    found: set[str] = set()
    for m in _XEON_E.finditer(text):
        series, num, suffix, vn = m.groups()
        found.add(f"E{series}-{num}{suffix}" + (f" V{vn}" if vn else ""))
    for m in _XEON_SCALABLE.finditer(text):
        found.add(f"{m.group(1)} {m.group(2)}")
    for m in _XEON_SCALABLE_HP.finditer(text):
        found.add(f"{_HP_SCALABLE_LETTER[m.group(1)]} {m.group(2)}")
    for m in _CORE_I_MODEL.finditer(text):
        found.add(f"I{m.group(1)}-{m.group(2)}{m.group(3)}")
    for m in _EPYC_MODEL.finditer(text):
        found.add(f"EPYC {m.group(1)}")
    for m in _RYZEN_MODEL.finditer(text):
        found.add(f"RYZEN {m.group(1)} {m.group(2)}")
    return found


def _family(text: str) -> str | None:
    """Seeded family member from explicit family signals, or None on conflict."""
    members: set[str] = set()
    if _XEON_WORD.search(text) or _XEON_E.search(text) or _XEON_SCALABLE.search(text) or _XEON_SCALABLE_HP.search(text):
        members.add(XEON)
    if _CORE_I_WORD.search(text) or _CORE_I_MODEL.search(text):
        members.add(CORE_I)
    if _EPYC_WORD.search(text):
        members.add(EPYC)
    if _THREADRIPPER_WORD.search(text):
        # Subsumption: the official name is "Ryzen Threadripper" — the RYZEN word
        # is part of the Threadripper brand, not a conflict (mirrors gpu.py's
        # GEFORCE-absorbs-GTX rule).
        members.add(THREADRIPPER)
    elif _RYZEN_WORD.search(text):
        members.add(RYZEN)
    if _ATOM_WORD.search(text):
        members.add(ATOM)
    return unique_or_none(members)


def _is_boost_clock(text: str, start: int, end: int) -> bool:
    """True when a GHz match is a turbo/boost figure ("up to 4.40 GHz", "(3.2GHz
    Turbo)") — never the base clock."""
    if text[:start].endswith("UP TO "):
        return True
    return text[end : end + 8].lstrip(" )").startswith("TURBO")


def _clock_ghz(text: str) -> float | None:
    """Distinct surviving base-clock candidate in the seeded range, or None."""
    values: set[float] = set()
    for rx in (_GHZ, _GHZ_BARE_G):
        for m in rx.finditer(text):
            if not _is_boost_clock(text, m.start(), m.end()):
                values.add(float(m.group(1)))
    for m in _GHZ_UNDERSCORE.finditer(text):
        if not _is_boost_clock(text, m.start(), m.end()):
            values.add(float(f"{m.group(1)}.{m.group(2)}"))
    values = {v for v in values if _GHZ_MIN <= v <= _GHZ_MAX}
    return unique_or_none(values)


def _core_count(text: str) -> int | None:
    """Distinct surviving core-count candidate in the seeded range, or None."""
    values = {int(m.group(1)) for rx in (_CORES_GLUED, _CORES_WORD) for m in rx.finditer(text)}
    values = {v for v in values if _CORE_MIN <= v <= _CORE_MAX}
    return unique_or_none(values)


def _tdp_watts(text: str) -> int | None:
    """Distinct surviving TDP candidate in the seeded range, or None."""
    values = {int(m.group(1)) for m in _TDP.finditer(text)}
    values = {v for v in values if _TDP_MIN <= v <= _TDP_MAX}
    return unique_or_none(values)


def _architecture(text: str, model: str | None) -> str | None:
    """Architecture by precedence: HP codename → full Intel name → vN suffix map.

    (The model→spec table is the final fallback, applied by the caller's merge.)
    Two DIFFERENT codename/name tokens ⇒ omit.
    """
    members = {_CODENAME_ARCH[m.group(1)] for m in _CODENAME.finditer(text)}
    members |= {_ARCH_NAMES[m.group(1)] for m in _ARCH_NAME.finditer(text)}
    arch = unique_or_none(members)
    if arch is not None or members:
        return arch  # explicit tokens win; a token CONFLICT omits (never falls through)
    if model is not None and model.startswith(("E3-", "E5-", "E7-")):
        return _VN_ARCH.get(model[-2:]) if " V" in model else None
    return None


def extract_cpu(text: str) -> SpecDict:
    """Extract cpu specs from an upper-cased, whitespace-collapsed description.

    The curated model→spec table fills facets the tokens don't state, merged UNDER
    directly-extracted values (a desc-stated GHz/core-count beats the table). socket
    comes from the table only (descriptions carry it on ~15 corpus rows — too rare to
    grammar).
    """
    text = _clean(text)
    model = unique_or_none(_models(text))

    specs: SpecDict = {}
    if model is not None:
        table = load_model_specs().get(model)
        if table:
            specs.update({k: table[k] for k in _TABLE_KEYS if k in table})

    family = _family(text)
    if family is not None:
        specs["family"] = family
    clock = _clock_ghz(text)
    if clock is not None:
        specs["clock_speed_ghz"] = clock
    # Bare numeric tokens (cores/TDP) need an explicit CPU signal — a GHz clock,
    # a model/family/codename hit, or a CPU word (see _CPU_CONTEXT).
    context = (
        bool(specs)
        or model is not None
        or bool(_CPU_CONTEXT.search(text))
        or bool(_CODENAME.search(text))
        or bool(_ARCH_NAME.search(text))
    )
    if context:
        cores = _core_count(text)
        if cores is not None:
            specs["core_count"] = cores
        tdp = _tdp_watts(text)
        if tdp is not None:
            specs["tdp_watts"] = tdp
    arch = _architecture(text, model)
    if arch is not None:
        specs["architecture"] = arch
    return specs

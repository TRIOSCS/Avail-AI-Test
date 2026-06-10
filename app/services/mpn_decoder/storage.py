"""Deterministic HDD MPN decoders (Seagate, Western Digital, Toshiba, HGST/Hitachi).

CONSERVATIVE by design: form_factor comes from the vendor prefix (reliable); capacity is
decoded only where the scheme expresses it unambiguously; usage_class only for well-known
family codes. Anything uncertain is omitted, never guessed. RPM/interface are not reliably
encoded in these MPNs, so they are left to later phases. SSD part-number schemes live in
the sibling ssd.py (WD WDS…, Samsung MZ…, Kioxia, …); Seagate XA/ZA/Nytro remain undecoded.

Legacy-era grammars (facet-accuracy audit 2026-06-10): the legacy WD decimal-GB scheme
(WD800BB = 80 GB) IS decoded — its exactly-2-letter suffix is a certain era gate; legacy
Seagate ST<ff><digits><iface> shapes and STMicroelectronics ST-prefixed order codes return
None (the Seagate gate now requires the modern 0-led structured tail). A wrong capacity is
worse than a missing one.
"""

import re

from app.services.mpn_decoder._common import DecodeResult

# Canonical enum strings — MUST match commodity_seeds.json exactly.
FF_35 = '3.5"'
FF_25 = '2.5"'
UC_ENTERPRISE = "Enterprise / Datacenter"
UC_NAS = "NAS"
UC_SURVEILLANCE = "Surveillance"
UC_DESKTOP = "Desktop / Client"

# ── Seagate ──────────────────────────────────────────────────────────────
# Modern family-coded scheme ONLY: ST<capacityGB><FAMILY><0-led tail>, e.g.
# ST4000NM0035 (4 TB), ST16000NM001G (16 TB), ST1000DM010 (1 TB), ST500LM030
# (500 GB), ST300MM0006 (300 GB 2.5" SAS). The structured tail is the era gate:
# every modern model ends <2 family letters> + a 3-4 char alphanumeric tail that
# STARTS WITH "0" ("0035", "010", "001G", "0006"), anchored to end-of-string.
# Legacy schemes cannot produce that shape:
#   * old ST<ff-digit><digits><iface letters> models END at the interface letters
#     (ST39103FC, ST373207LC, ST973402SS; ST3500418AS additionally has a digit
#     glued to the letters), so they return None — their digit string mixes a
#     form-factor digit with MB digits, and pre-~1996 models encode UNFORMATTED
#     MB (ST3600N is a 525 MB drive), so no pattern-only grammar can split those
#     eras with certainty. A wrong capacity is worse than a missing one.
#   * STMicroelectronics order codes (ST232BDR, ST3232EBDR, ST485, STM32…) end in
#     package/reel letters (D/N/P/W/T + R), never a 0-led tail — the ST-prefix
#     collision that once decoded an RS-232 transceiver as a "232 GB drive" is
#     structurally excluded, and _STMICRO_DENY below rejects those shapes outright
#     as defense-in-depth (a future loosening of the accept gate must not silently
#     re-admit the IC class).
_SEAGATE = re.compile(r"^ST(\d{3,6})([A-Z]{2})(0[A-Z0-9]{2,3})$")
# STMicro order-code deny-shapes: STM32/STM8 MCU prefixes, and short ST<digits>
# codes ending in 0-4 package letters (+ optional reel "R") with no structured
# tail (ST232BDR, ST485, ST3232EBDR). Real Seagate models never fit: their family
# letters are always followed by the 0-led tail digits.
_STMICRO_DENY = re.compile(r"^ST(?:M\d|\d{2,4}[A-Z]{0,4}R?$)")
_SEAGATE_FAMILY = {
    "NM": (FF_35, UC_ENTERPRISE),  # Exos / Constellation enterprise
    "NE": (FF_35, UC_NAS),  # IronWolf Pro
    "VN": (FF_35, UC_NAS),  # IronWolf
    "VX": (FF_35, UC_SURVEILLANCE),  # SkyHawk
    "SV": (FF_35, UC_SURVEILLANCE),
    "DM": (FF_35, UC_DESKTOP),  # BarraCuda
    "DX": (FF_35, UC_DESKTOP),  # FireCuda 3.5"
    "LM": (FF_25, UC_DESKTOP),  # BarraCuda 2.5"
    "LX": (FF_25, UC_DESKTOP),  # FireCuda 2.5"
}


def _seagate(mpn: str) -> DecodeResult | None:
    if _STMICRO_DENY.match(mpn):
        return None
    m = _SEAGATE.match(mpn)
    if not m:
        return None
    specs: dict = {"capacity_gb": int(m.group(1))}
    fam = _SEAGATE_FAMILY.get(m.group(2))
    if fam:
        specs["form_factor"], specs["usage_class"] = fam
    return DecodeResult(commodity="hdd", vendor="Seagate", specs=specs)


# ── Western Digital (HDD) ────────────────────────────────────────────────
# TWO WD HDD grammars, era-split by the SUFFIX SHAPE (the digits alone are ambiguous):
#
# LEGACY decimal-GB scheme — WD<digits><EXACTLY two family letters> (then end or a
# "-revision"): the digits are GB with an implied decimal before the last digit.
# WD800BB = 80.0 GB, WD600BB = 60.0 GB, WD2500JB = 250 GB, WD360GD = 36 GB Raptor,
# WD64AA = 6.4 GB. Every 2-letter WD family code (AA/BA/BB/JB/EB/GD/JD/KS/YR/UE…)
# belongs to this pre-TB era — the TB era (WD10EACS onward, 2007+) always uses
# 4-letter codes — so the exactly-2-letter suffix is a certain era gate. The naive
# "digits × 100" read here was the audit's 1000× class (WD800BB → 80,000 GB).
# 2-letter codes span 3.5" (BB/JB) and 2.5" (UE) lines, so legacy emits capacity ONLY.
#
# MODERN TB×10 scheme — WD<2-3 digits><4+ letter family code>: digits/10 = TB
# (WD40EFRX = 4 TB, WD140EFGX = 14 TB). Capacity is emitted only when the era is
# certain: a 2-digit form is always TB-era (the GB era's 4-letter-code parts were all
# ≥36 GB ⇒ ≥3 digits), and a 3-digit form is TB-era only when the suffix carries a
# recognized modern family token from _WD_FAMILY — without one, a 3-digit+4-letter
# shape is ambiguous between eras (WD800AAJS = 80 GB vs WD140EFGX = 14 TB) and
# returns None. The old 4-digit mixed scheme (WD5000AAKX = 500 GB, WD1002FAEX =
# 1 TB — same shape, different units) never matches either gate.
#
# form_factor is taken ONLY from a recognized 3.5" family code (every entry in
# _WD_FAMILY is a 3.5" line). The suffix's first letter is NOT a reliable form-factor
# signal — 2.5" mobile drives use varied codes (WD10JPLX, WD…LPLX, WD…LPCX) that
# don't start "S", so the old "S ⇒ 2.5"" rule mislabeled them 3.5". When no family
# matches (but capacity is era-certain) we emit capacity only. SSDs (WDS…) start
# with a letter and match neither gate.
_WD_LEGACY = re.compile(r"^WD(\d{2,4})([A-Z]{2})(?![A-Z])")
_WD_MODERN = re.compile(r"^WD(\d{2,3})([A-Z]{4,})")
_WD_FAMILY = [  # ordered substring → usage_class (first match wins)
    ("EFR", UC_NAS),
    ("EFA", UC_NAS),
    ("EFZ", UC_NAS),
    ("EFG", UC_NAS),
    ("EFP", UC_NAS),
    ("EFB", UC_NAS),
    ("EFC", UC_NAS),
    ("PUR", UC_SURVEILLANCE),
    ("PUZ", UC_SURVEILLANCE),
    ("FRY", UC_ENTERPRISE),
    ("FBY", UC_ENTERPRISE),
    ("FYY", UC_ENTERPRISE),
    ("WUS", UC_ENTERPRISE),
    ("WUH", UC_ENTERPRISE),
    ("EZE", UC_DESKTOP),
    ("EZR", UC_DESKTOP),
    ("EZA", UC_DESKTOP),
    ("FZE", UC_DESKTOP),
    ("FZB", UC_DESKTOP),
]


def _wd(mpn: str) -> DecodeResult | None:
    m = _WD_LEGACY.match(mpn)
    if m:
        # Legacy decimal-GB era (see the grammar comment): digits / 10 = GB.
        value = int(m.group(1)) / 10
        capacity = int(value) if value == int(value) else value
        return DecodeResult(commodity="hdd", vendor="Western Digital", specs={"capacity_gb": capacity})
    m = _WD_MODERN.match(mpn)
    if not m:
        return None
    digits, suffix = m.group(1), m.group(2)
    specs: dict = {}
    for token, uc in _WD_FAMILY:
        if token in suffix:
            specs["usage_class"] = uc
            specs["form_factor"] = FF_35  # every family in _WD_FAMILY is a 3.5" line
            break
    # TB×10 capacity only when the era is certain: any 2-digit form, or a 3-digit
    # form whose suffix carries a recognized modern family token. An unrecognized
    # 3-digit+4-letter shape (WD800AAJS = 80 GB legacy) stays ambiguous → no specs.
    if len(digits) == 2 or "usage_class" in specs:
        specs["capacity_gb"] = int(digits) * 100  # TB×10 → GB
    if not specs:
        return None
    return DecodeResult(commodity="hdd", vendor="Western Digital", specs=specs)


# ── Toshiba (HDD) ────────────────────────────────────────────────────────
# Prefix → form factor + (where known) usage class. Capacity from an explicit "<n>T" token
# when present (e.g. MG08ACA16TE = 16 TB).
# Gate requires the full Toshiba family structure — prefix + 2 digits + a 3-letter family
# code (MG08ACA…, DT01ACA…, MQ01ABD…). A bare 2-char prefix matched too broadly: short OEM
# spare numbers like Dell DPNs "MGK50"/"MGJN9" (no \d{2}) and "DT10171…" (digits, not 3
# letters, after the prefix) were mis-decoded as drives. The structured gate excludes them.
_TOSHIBA = re.compile(r"^(MG|MN|MD|MQ|DT)\d{2}[A-Z]{3}")
_TOSHIBA_PREFIX = {
    "MG": (FF_35, UC_ENTERPRISE),
    "MN": (FF_35, UC_NAS),
    "MD": (FF_35, UC_ENTERPRISE),
    "MQ": (FF_25, None),
    "DT": (FF_35, UC_DESKTOP),
}
_TB_TOKEN = re.compile(r"(\d{1,2})T[A-Z]?(?:\b|$)")


def _toshiba(mpn: str) -> DecodeResult | None:
    m = _TOSHIBA.match(mpn)
    if not m:
        return None
    info = _TOSHIBA_PREFIX.get(m.group(1))
    specs: dict = {}
    if info:
        specs["form_factor"] = info[0]
        if info[1]:
            specs["usage_class"] = info[1]
    cap = _TB_TOKEN.search(mpn)
    if cap:
        specs["capacity_gb"] = int(cap.group(1)) * 1000  # TB → GB
    return DecodeResult(commodity="hdd", vendor="Toshiba", specs=specs) if specs else None


# ── HGST / Hitachi (HDD) ─────────────────────────────────────────────────
# HUS requires a digit next (Ultrastar HDDs: HUS72…, HUS156…). The letter-suffixed HUS
# families are Ultrastar *SSDs* (HUSMM/HUSSL/HUSMR SAS, HUSPR PCIe NVMe SN100/SN150) —
# not 3.5" HDDs — so the bare ^HUS gate mislabeled them; they now return None rather
# than a wrong HDD decode.
_HGST = re.compile(r"^(HUH|HUS(?=\d)|HUC|HTS|HDN|HDS|HMS)")
_HGST_PREFIX = {
    "HUH": (FF_35, UC_ENTERPRISE),  # Ultrastar He
    "HUS": (FF_35, UC_ENTERPRISE),  # Ultrastar
    "HUC": (FF_25, UC_ENTERPRISE),  # Ultrastar 2.5"
    "HTS": (FF_25, None),  # Travelstar
    "HDN": (FF_35, UC_NAS),  # Deskstar NAS
    "HDS": (FF_35, UC_DESKTOP),  # Deskstar
    "HMS": (FF_35, UC_DESKTOP),  # Deskstar / CinemaStar 3.5"
}


def _hgst(mpn: str) -> DecodeResult | None:
    m = _HGST.match(mpn)
    if not m:
        return None
    info = _HGST_PREFIX.get(m.group(1))  # defensive: never KeyError if the gate drifts
    if info is None:
        return None
    specs: dict = {"form_factor": info[0]}
    if info[1]:
        specs["usage_class"] = info[1]
    cap = _TB_TOKEN.search(mpn)
    if cap:
        specs["capacity_gb"] = int(cap.group(1)) * 1000
    return DecodeResult(commodity="hdd", vendor="HGST", specs=specs)


_STORAGE_DECODERS = (_seagate, _wd, _toshiba, _hgst)


def decode_storage(mpn: str, manufacturer: str | None = None) -> DecodeResult | None:
    """Decode a storage-drive MPN (already upper-cased) or return None."""
    for decoder in _STORAGE_DECODERS:
        result = decoder(mpn)
        if result is not None and result.specs:
            return result
    return None

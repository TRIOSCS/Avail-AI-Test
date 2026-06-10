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

Re-audit round 2 (2026-06-10): three more deterministic gates on top of the above —
(1) WD's MODERN scheme reads the final digit of the numeric group as a revision/
    generation marker, never fractional capacity (WD42PURZ = 4 TB rev 2, WD101EFBX =
    10 TB rev 1 — the round-1 TB×10 read produced 4.2/10.1 TB ghosts);
(2) every Seagate modern-family capacity must sit inside a per-family envelope
    (_SEAGATE_ENVELOPE) — out-of-envelope means a truncated/malformed string, NO decode;
(3) every hdd capacity any decoder here emits must land on the discrete grid of
    capacities vendors actually shipped (HDD_SHIPPED_CAPACITY_GB) — an off-grid value
    is moved to DecodeResult.dropped (never specs) so writer.py can WARN about it.
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
# Capacity group is 3-5 digits (100 GB … 99 TB): no shipped Seagate model needs six
# (a 6-digit read would be ≥100 TB — always a malformed string), part of the re-audit's
# strict length/digit-count validation.
_SEAGATE = re.compile(r"^ST(\d{3,5})([A-Z]{2})(0[A-Z0-9]{2,3})$")
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

# Per-family shipped-capacity envelopes in GB (re-audit 2026-06-10, residual class 2):
# a digit-dropped truncation slips the structured-tail shape gate ("ST120MM0198", from
# the real 1.2 TB ST1200MM0198, decoded 120 GB), so the SHAPE alone is not enough — the
# capacity must also be consistent with the family's actual product range. Envelopes are
# deliberately WIDE (their job is catching the ≥10× truncation class, not enumerating
# SKUs — the discrete HDD_SHIPPED_CAPACITY_GB grid below handles off-ladder points), but
# every bound is anchored to real launch/EOL SKUs. A family WITHOUT a vetted envelope
# returns None outright: a capacity we cannot range-check is a best-effort guess, and a
# wrong capacity is worse than a missing one. The closed table also keeps Seagate's
# modern-shaped SAS *SSD* families (FM Nytro, FP Pulsar — e.g. ST400FM0233) from ever
# taking an hdd decode: they are deliberately NOT listed.
_SEAGATE_ENVELOPE = {
    "NM": (500, 32000),  # Constellation ES 500 GB → Exos X (incl. 28-32 TB HAMR/SMR)
    "MM": (300, 2400),  # Savvio / Enterprise Performance 10K-15K 2.5" SAS: 300 GB-2.4 TB
    "MP": (300, 900),  # Enterprise Performance 15K 2.5" SAS: 300/600/900 GB
    "NX": (500, 4000),  # Exos 7E 2.5" nearline
    "NE": (1000, 32000),  # IronWolf Pro
    "NT": (1000, 32000),  # IronWolf Pro (20 TB+ NT tails)
    "VN": (1000, 32000),  # IronWolf
    "VX": (500, 24000),  # SkyHawk
    "VE": (1000, 32000),  # SkyHawk AI
    "VM": (500, 4000),  # Video 3.5" (Pipeline successor)
    "SV": (250, 8000),  # SV35 surveillance
    "DM": (250, 16000),  # BarraCuda 3.5" (ST250DM000 → BarraCuda Pro 14 TB)
    "DX": (500, 8000),  # FireCuda 3.5" SSHD
    "DL": (500, 3000),  # Barracuda Green
    "LM": (160, 5000),  # 2.5" Momentus/BarraCuda (ST160LM003 → ST5000LM000)
    "LX": (250, 4000),  # FireCuda 2.5" SSHD
    "LT": (250, 1000),  # Laptop Thin 2.5"
    "AS": (5000, 8000),  # Archive SMR (modern 0-led-tail AS only; legacy ...AS never matches)
}


def _seagate(mpn: str) -> DecodeResult | None:
    if _STMICRO_DENY.match(mpn):
        return None
    m = _SEAGATE.match(mpn)
    if not m:
        return None
    capacity, family = int(m.group(1)), m.group(2)
    envelope = _SEAGATE_ENVELOPE.get(family)
    if envelope is None or not envelope[0] <= capacity <= envelope[1]:
        # Unknown family (no vetted range) or out-of-envelope capacity (truncated/
        # malformed string): NO decode at all — never a best-effort capacity, and the
        # form/usage of a string we distrust is equally untrustworthy.
        return None
    specs: dict = {"capacity_gb": capacity}
    fam = _SEAGATE_FAMILY.get(family)
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
# MODERN revision-digit scheme — WD<2-3 digits><4+ letter family code>: the FINAL
# digit of the numeric group is a revision/generation marker, NEVER fractional
# capacity — capacity_TB = the leading digits (re-audit 2026-06-10, residual class 1):
# WD40PURZ = 4 TB and WD42PURZ = 4 TB rev 2 (not 4.2); WD100EFAX = 10 TB and
# WD101EFBX = 10 TB rev 1 (not 10.1); WD121PURP = 12 TB; WD22PURZ = 2 TB. The
# round-1 "digits/10 = TB" read was only correct for rev-0 parts and minted 1-5%-off
# ghost capacities (4.2/10.1/12.1/2.2 TB — points no vendor ever shipped) for the rest.
# SOLE exception, _WD_FRACTIONAL_TB10: the Caviar-Green-era fractional points that
# really shipped — WD15…(EADS/EARS/EARX/NPVT) = 1.5 TB and WD25…(EZRS/NPVT) = 2.5 TB.
# No WD revision has ever reached 5, so digits 15/25 read fractional, deterministically;
# if WD ever ships a rev-5 part, that family must be split out of this map by suffix.
# Capacity is emitted only when the era is certain: a 2-digit form is always TB-era
# (the GB era's 4-letter-code parts were all ≥36 GB ⇒ ≥3 digits), and a 3-digit form
# is TB-era only when the suffix carries a recognized modern family token from
# _WD_FAMILY — without one, a 3-digit+4-letter shape is ambiguous between eras
# (WD800AAJS = 80 GB vs WD140EFGX = 14 TB) and returns None. The old 4-digit mixed
# scheme (WD5000AAKX = 500 GB, WD1002FAEX = 1 TB — same shape, different units) never
# matches either gate.
#
# form_factor is taken ONLY from a recognized 3.5" family code (every entry in
# _WD_FAMILY is a 3.5" line). The suffix's first letter is NOT a reliable form-factor
# signal — 2.5" mobile drives use varied codes (WD10JPLX, WD…LPLX, WD…LPCX) that
# don't start "S", so the old "S ⇒ 2.5"" rule mislabeled them 3.5". When no family
# matches (but capacity is era-certain) we emit capacity only. SSDs (WDS…) start
# with a letter and match neither gate.
_WD_LEGACY = re.compile(r"^WD(\d{2,4})([A-Z]{2})(?![A-Z])")
_WD_MODERN = re.compile(r"^WD(\d{2,3})([A-Z]{4,})")
# Shipped fractional-TB points (digits → GB) — see the modern-scheme comment above.
_WD_FRACTIONAL_TB10 = {"15": 1500, "25": 2500}
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
    # Capacity only when the era is certain: any 2-digit form, or a 3-digit form
    # whose suffix carries a recognized modern family token. An unrecognized
    # 3-digit+4-letter shape (WD800AAJS = 80 GB legacy) stays ambiguous → no specs.
    if len(digits) == 2 or "usage_class" in specs:
        if digits in _WD_FRACTIONAL_TB10:  # WD15/WD25 Green-era 1.5/2.5 TB points
            specs["capacity_gb"] = _WD_FRACTIONAL_TB10[digits]
        else:
            # Revision-digit rule: drop the final digit, leading digits are TB.
            specs["capacity_gb"] = int(digits[:-1]) * 1000
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

# ── Shipped-capacity grid (re-audit 2026-06-10, classes 1+2 backstop) ───────────
# HDD capacities are a DISCRETE vendor vocabulary, not a continuum: every drive ever
# marketed sits on a small ladder of points, so ANY decode landing off the ladder is a
# decoder bug (revision digit read as tenths, digit-dropped truncation, …), never a
# real drive. 10.1 / 12.1 / 4.2 / 2.2 TB — the four re-audit errors — are all off-grid,
# and being only 1-5% off they can never be caught by a magnitude ceiling; the discrete
# vocabulary is the right gate. Provenance per ladder below; built conservatively from
# capacities with attested retail/OEM SKUs — a real-but-unlisted capacity costs a
# MISSING facet (acceptable), an off-grid value written would be a WRONG one (never).
#
# HDD ONLY — deliberately NOT applied to the ssd.py decoders: SSD capacities are
# near-continuous (binary 256/512/1024 GB, decimal 250/500/1000, overprovisioned
# enterprise 400/800/1600/3200/6400, TLC-era 480/960/1920/3840/7680/15360/30720, …),
# so a useful grid would have to enumerate nearly every value and could only drop real
# parts; the SSD schemes also encode capacity as an explicit size field rather than a
# free digit-string read, so the failure class this grid back-stops does not exist
# there.
HDD_SHIPPED_CAPACITY_GB = frozenset(
    {
        # late-90s IDE decimal-GB ladder (industry-wide points: WD Caviar AA/BA,
        # Quantum Fireball, Seagate Medalist all shipped these exact sizes)
        2.1,
        3.2,
        4.3,
        6.4,
        8.4,
        # 2000s IDE/early-SATA + mobile ladder (WD decimal-GB 2-letter era: WD102AA =
        # 10.2, WD136AA = 13.6, WD172AA = 17.2, WD205BB = 20.5, WD307AA = 30.7,
        # WD450BB = 45; Protégé WD100/150EB = 10/15; Raptor WD360GD = 36, WD740GD = 74,
        # Raptor-X 150; round-GB Caviar/Scorpio 20…500; perpendicular-era 640/750)
        10,
        10.2,
        13.6,
        15,
        17.2,
        20,
        20.5,
        27.2,
        30,
        30.7,
        32,
        36,
        40,
        45,
        60,
        74,
        80,
        100,
        120,
        150,
        160,
        180,
        200,
        250,
        300,
        320,
        400,
        500,
        640,
        750,
        # parallel-SCSI enterprise ladder (Cheetah/Atlas/Ultrastar 10K-15K)
        9.1,
        18.4,
        36.4,
        73.4,
        146,
        # SAS enterprise 2.5"/3.5" ladder (Savvio / Enterprise Performance / MM-series;
        # 300/600 shared with the lists above)
        450,
        600,
        900,
        1200,
        1800,
        2400,
        # TB-era 3.5" CMR/SMR/He grid; 1500/2500 are the shipped Caviar-Green fractional
        # points (WD15EADS/EARS, WD25EZRS); 7/9/11/13/15/17/19/21/23/25 TB never shipped;
        # 28000-32000 cover the 2024+ UltraSMR/HAMR ships (WD 28/32 TB, Exos M 30/32 TB)
        1000,
        1500,
        2000,
        2500,
        3000,
        4000,
        5000,
        6000,
        8000,
        10000,
        12000,
        14000,
        16000,
        18000,
        20000,
        22000,
        24000,
        26000,
        28000,
        30000,
        32000,
    }
)


def decode_storage(mpn: str, manufacturer: str | None = None) -> DecodeResult | None:
    """Decode a storage-drive MPN (already upper-cased) or return None."""
    for decoder in _STORAGE_DECODERS:
        result = decoder(mpn)
        if result is None or not result.specs:
            continue
        capacity = result.specs.get("capacity_gb")
        if capacity is not None and capacity not in HDD_SHIPPED_CAPACITY_GB:
            # Off the shipped-capacity grid ⇒ the capacity read is wrong even though
            # the shape gates passed. Move it to `dropped` (never specs) so writer.py
            # surfaces it in the aggregate drop-WARNING instead of persisting it.
            result.dropped["capacity_gb"] = result.specs.pop("capacity_gb")
        if result.specs:
            return result
        # The grid emptied the decode (capacity was its only spec): treat as no decode
        # — the vendor gates are mutually exclusive, so no later decoder can match.
        return None
    return None

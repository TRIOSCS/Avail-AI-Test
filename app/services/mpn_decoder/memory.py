"""Deterministic DRAM-module MPN decoders (Samsung, SK Hynix, Micron, Kingston,
Crucial).

CONSERVATIVE: decodes generation / form factor / ECC from the documented module-type codes,
and capacity / speed only where the scheme expresses them cleanly (Kingston `/<cap>`,
Crucial `CT<cap>G…`). Density/speed codes that vary or are ambiguous are skipped, never
guessed. Values map to the seeded `dram` facet keys.

Round 2 adds three keys, emitted ONLY where the module code deterministically encodes them:
- `rank`       — exact strings 1Rx4/1Rx8/2Rx4/2Rx8/4Rx4 (8Rx4 reserved): explicit org-token
                 tables (Samsung), device-count math (Hynix/Micron), S/D/Q tokens (Kingston),
                 F[SDQ][48] tokens (Crucial). Ambiguous org codes (3DS stacks, Samsung 8G40)
                 emit nothing.
- `registered` — Registered/Unbuffered/Load-Reduced, 1:1 from the module type that already
                 yields form_factor. (Fully-Buffered is reserved for FB-DIMM schemes we do
                 not decode — DDR2-era parts never match these gates.)
- `voltage`    — 1.2 (all JEDEC DDR4), 1.5/1.35 for DDR3 only where the scheme marks low
                 voltage (Samsung -C/H vs -Y suffix, Micron JSF vs KSF, Kingston L flag).
                 DDR5 (1.1 V) is out of the seeded vocabulary, so it is omitted.
"""

import re

from app.services.mpn_decoder._common import DecodeResult

# Canonical dram enum strings — MUST match commodity_seeds.json exactly.
RDIMM, LRDIMM, UDIMM, SODIMM, DIMM = "RDIMM", "LRDIMM", "UDIMM", "SO-DIMM", "DIMM"
DDR3, DDR4, DDR5 = "DDR3", "DDR4", "DDR5"
REG_R, REG_U, REG_LR = "Registered", "Unbuffered", "Load-Reduced"
V12, V135, V15 = 1.2, 1.35, 1.5

# Buffering is a 1:1 consequence of the module type — emitted alongside form_factor.
_REGISTERED_BY_FORM = {RDIMM: REG_R, LRDIMM: REG_LR, UDIMM: REG_U, SODIMM: REG_U}
# rank values must be one of the exact seeded strings — anything else is dropped.
_ALLOWED_RANKS = {"1Rx4", "1Rx8", "2Rx4", "2Rx8", "4Rx4", "8Rx4"}


def _r(commodity_specs: dict, vendor: str) -> DecodeResult | None:
    return DecodeResult(commodity="dram", vendor=vendor, specs=commodity_specs) if commodity_specs else None


def _set_form(specs: dict, form: str) -> None:
    specs["form_factor"] = form
    registered = _REGISTERED_BY_FORM.get(form)
    if registered:
        specs["registered"] = registered


# ── Samsung (M<code><gen>…) ──────────────────────────────────────────────
# module-code → (form_factor, ecc); generation from the letter after the code, or the
# explicit DDR5 codes.
_SAMSUNG = re.compile(r"^M(\d{3})([A-Z])")
_SAMSUNG_MODULE = {
    "393": (RDIMM, True),
    "386": (LRDIMM, True),
    "391": (UDIMM, True),
    "378": (UDIMM, False),
    "471": (SODIMM, False),
    "474": (SODIMM, True),
    "321": (RDIMM, True),
    "323": (UDIMM, False),
    "425": (SODIMM, False),  # DDR5 lines
}
_SAMSUNG_DDR5_CODES = {"321", "323", "425"}
# DDR4 org block (the 4 chars after the generation letter, e.g. M393A|2K43|DB3-CWE):
# digit 1 = module density (1=8GB, 2=16GB, 4=32GB, 8=64GB), letter = die density code,
# digit 3 ("4") = device generation, digit 4 = device width (0=x4, 3=x8). Rank is an
# explicit verified-token table — formula decoding is unsafe because the die letter is
# era-dependent (8G40 is 2Rx4-16Gb or 4Rx4-3DS depending on vintage → deliberately absent).
_SAMSUNG_ORG = re.compile(r"^M\d{3}[A-Z](\d[A-Z]4\d)")
_SAMSUNG_ORG_RANK = {
    "1K43": "1Rx8",
    "2K43": "2Rx8",
    "2K40": "1Rx4",
    "2G40": "2Rx4",
    "4K40": "2Rx4",
    "4G43": "2Rx8",
    "8K40": "4Rx4",
}
_SAMSUNG_DDR4_GB = {"1": 8, "2": 16, "4": 32, "8": 64}
# DDR3 voltage is the first letter of the speed-bin suffix: -C…/-H… = 1.5 V, -Y… = 1.35 V
# (e.g. M378B5273DH0-CK0 vs M393B1K70DH0-YH9). DDR4 is always JEDEC 1.2 V.
_SAMSUNG_SUFFIX = re.compile(r"-([A-Z])")
_SAMSUNG_DDR3_VOLT = {"C": V15, "H": V15, "Y": V135}


def _samsung(mpn: str) -> DecodeResult | None:
    m = _SAMSUNG.match(mpn)
    if not m:
        return None
    mod = _SAMSUNG_MODULE.get(m.group(1))
    if not mod:
        return None
    specs: dict = {"ecc": mod[1]}
    _set_form(specs, mod[0])
    if m.group(1) in _SAMSUNG_DDR5_CODES:
        specs["ddr_type"] = DDR5  # 1.1 V — not in the seeded voltage vocabulary, omitted
    elif m.group(2) == "A":
        specs["ddr_type"] = DDR4
        specs["voltage"] = V12
        org = _SAMSUNG_ORG.match(mpn)
        if org:
            token = org.group(1)
            gb = _SAMSUNG_DDR4_GB.get(token[0])
            if gb:
                specs["capacity_gb"] = gb
            rank = _SAMSUNG_ORG_RANK.get(token)
            if rank:
                specs["rank"] = rank
    elif m.group(2) == "B":
        specs["ddr_type"] = DDR3
        suffix = _SAMSUNG_SUFFIX.search(mpn)
        if suffix:
            volt = _SAMSUNG_DDR3_VOLT.get(suffix.group(1))
            if volt:
                specs["voltage"] = volt
    return _r(specs, "Samsung")


# ── SK Hynix (HM<gen>…<R/U/S/L><6|7>…) ───────────────────────────────────
_HYNIX = re.compile(r"^HM(CG|CT|A|T)")
_HYNIX_GEN = {"T": DDR3, "A": DDR4, "CG": DDR5, "CT": DDR5}
_HYNIX_FF = re.compile(r"[0-9G]([RULS])([678])")
_HYNIX_FORM = {"R": (RDIMM, True), "L": (LRDIMM, True), "U": (UDIMM, None), "S": (SODIMM, None)}
# DDR4 org block HMA<die><mult>G<R|U><bits>…<width>N (e.g. HMA84GR7AFR4N-UH):
# die = component density (4=4Gb, 8=8Gb, A=16Gb), mult = capacity/8GB, width = x4/x8.
# A x4 rank carries 16 data devices (2×die GB), a x8 rank 8 (1×die GB), so
# ranks = capacity / per-rank-GB. Restricted to R/U modules — LRDIMM/3DS stacks report
# package ranks that this math does not model, so they emit nothing.
_HYNIX_DDR4_ORG = re.compile(r"^HMA([48A])(\d)G([RU])\d[A-Z]{3}([48])N")
_HYNIX_DIE_GB = {"4": 4, "8": 8, "A": 16}


def _hynix(mpn: str) -> DecodeResult | None:
    m = _HYNIX.match(mpn)
    if not m:
        return None
    gen = _HYNIX_GEN[m.group(1)]
    specs: dict = {"ddr_type": gen}
    if gen == DDR4:
        specs["voltage"] = V12  # JEDEC DDR4; DDR3 1.5/1.35 V split is not safely encoded
    ff = _HYNIX_FF.search(mpn)
    if ff:
        form, ecc = _HYNIX_FORM[ff.group(1)]
        _set_form(specs, form)
        if ecc is True:
            specs["ecc"] = True  # RDIMM/LRDIMM are always ECC
        elif ff.group(2) == "7":
            specs["ecc"] = True  # 72-bit ECC UDIMM/SODIMM
        elif ff.group(2) == "6":
            specs["ecc"] = False  # 64-bit non-ECC
    org = _HYNIX_DDR4_ORG.match(mpn)
    if org:
        capacity = int(org.group(2)) * 8
        specs["capacity_gb"] = capacity
        die_gb = _HYNIX_DIE_GB[org.group(1)]
        width = org.group(4)
        per_rank_gb = die_gb * 2 if width == "4" else die_gb
        ranks, remainder = divmod(capacity, per_rank_gb)
        rank = f"{ranks}Rx{width}"
        if remainder == 0 and rank in _ALLOWED_RANKS:
            specs["rank"] = rank
    return _r(specs, "SK Hynix")


# ── Micron ───────────────────────────────────────────────────────────────
# Structural module scheme MT[A]<devices><family><n>G<64|72><module letters>Z-<speed>:
#   MTA18ASF2G72PZ-2G6E1 (DDR4), MT36KSF2G72PZ-1G6M1 (DDR3). Decodes:
#   devices+bus → rank (9/72=1Rx8, 18/72=1Rx4, 36/72=2Rx4, 72/72=4Rx4 LRDIMM,
#     8/64=1Rx8, 16/64=2Rx8 — x4 is server/ECC-only so 16/64 is never 1Rx4);
#     two-letter module codes (…PDZ = 2Rx8 on an 18-device board) are rank-ambiguous
#     by device count alone → rank omitted, form factor still decoded;
#   <n>G<bus> → capacity_gb = n×8 (2G72 → 16GB);
#   module letter → form factor (P=RDIMM, A=UDIMM, L=LRDIMM, H/S=SO-DIMM);
#   DDR3 family letter → voltage (JSF = 1.5 V, KSF = DDR3L 1.35 V); DDR4 = 1.2 V.
# Legacy fallback: bare MTA/MTC prefixes still yield ddr_type + ecc only. Components
# (MT40A…) and schemes without a clean generation token (MT9HTF…) never decode.
_MICRON = re.compile(r"^MT([AC])\d")
_MICRON_ECC = re.compile(r"(72|64)")
_MICRON_DDR4_ORG = re.compile(r"^MTA(\d{1,2})[A-Z]{3}(\d{1,2})G(64|72)([A-Z]{1,2})Z(?=-|$)")
_MICRON_DDR3_ORG = re.compile(r"^MT(\d{1,2})([JK])SF(\d{1,2})G(64|72)([A-Z]{1,2})Z(?=-|$)")
_MICRON_RANK = {
    ("9", "72"): "1Rx8",
    ("18", "72"): "1Rx4",
    ("36", "72"): "2Rx4",
    ("72", "72"): "4Rx4",
    ("8", "64"): "1Rx8",
    ("16", "64"): "2Rx8",
}
_MICRON_FORM = {"P": RDIMM, "A": UDIMM, "L": LRDIMM, "H": SODIMM, "S": SODIMM}


def _micron_org(specs: dict, devices: str, density: str, bus: str, module: str) -> None:
    specs["ecc"] = bus == "72"
    specs["capacity_gb"] = int(density) * 8
    form = _MICRON_FORM.get(module[0])
    if form:
        _set_form(specs, form)
    if len(module) == 1:  # two-letter module variants change the org — rank only when unambiguous
        rank = _MICRON_RANK.get((devices, bus))
        if rank:
            specs["rank"] = rank


def _micron(mpn: str) -> DecodeResult | None:
    m = _MICRON_DDR3_ORG.match(mpn)
    if m:
        specs: dict = {"ddr_type": DDR3, "voltage": V15 if m.group(2) == "J" else V135}
        _micron_org(specs, m.group(1), m.group(3), m.group(4), m.group(5))
        return _r(specs, "Micron")
    m = _MICRON.match(mpn)
    if not m:
        return None
    specs = {"ddr_type": {"A": DDR4, "C": DDR5}[m.group(1)]}
    if m.group(1) == "A":
        specs["voltage"] = V12  # JEDEC DDR4 module; DDR5 (1.1 V) omitted
        org = _MICRON_DDR4_ORG.match(mpn)
        if org:
            _micron_org(specs, org.group(1), org.group(2), org.group(3), org.group(4))
            return _r(specs, "Micron")
    ecc = _MICRON_ECC.search(mpn)
    if ecc:
        specs["ecc"] = ecc.group(1) == "72"
    return _r(specs, "Micron")


# ── Kingston (KVR/KSM speed+module; trailing /<cap> across all lines) ─────
# KVR<speed><L?><module><CL><rank token>/<cap>, KSM<speed><module><rank token>/<cap>:
#   speed pins the generation (13/16/18 = DDR3, 21–32 = DDR4, 48–64 = DDR5) — the old
#   "D4 substring = DDR4" read was wrong: D4/S8/Q4 are Kingston's rank×width tokens
#   (D=dual, S=single, Q=quad), so KVR16R11D4/16 is a DDR3 2Rx4 RDIMM, not DDR4.
#   An L right after a DDR3 speed is the low-voltage flag (KVR16LR11D4 = DDR3L 1.35 V
#   RDIMM); after a DDR4/DDR5 speed, L is the LRDIMM module letter itself.
_KINGSTON = re.compile(r"^(KVR|KSM|KCP|KTH|KTD|KCS|KF|KSV)")
_KVR_KSM = re.compile(r"^(?:KVR|KSM)(\d{2})(L?)([A-Z]?)")
_KING_CAP = re.compile(r"[/-](\d{1,3})[A-Z]{0,4}$")  # tolerate die-rev suffixes (…/32HDR, …-32HA)
_KING_RANK = re.compile(r"([SDQ])([48])")
_KING_GEN = re.compile(r"D([345])")  # explicit DDRx token — fallback for KCP/KTH/KTD only
_KING_SPEED = {
    "13": 1333,
    "16": 1600,
    "18": 1866,
    "21": 2133,
    "24": 2400,
    "26": 2666,
    "29": 2933,
    "32": 3200,
    "42": 4200,
    "48": 4800,
    "52": 5200,
    "56": 5600,
    "64": 6400,
}
_KING_GEN_BY_SPEED = {
    "13": DDR3,
    "16": DDR3,
    "18": DDR3,
    "21": DDR4,
    "24": DDR4,
    "26": DDR4,
    "29": DDR4,
    "32": DDR4,
    "48": DDR5,
    "52": DDR5,
    "56": DDR5,
    "64": DDR5,
}
_KING_FORM = {"N": (UDIMM, False), "E": (UDIMM, True), "R": (RDIMM, True), "L": (LRDIMM, True), "S": (SODIMM, False)}
_KING_RANK_COUNT = {"S": "1", "D": "2", "Q": "4"}


def _kingston(mpn: str) -> DecodeResult | None:
    if not _KINGSTON.match(mpn):
        return None
    specs: dict = {}
    cap = _KING_CAP.search(mpn)
    if cap:
        specs["capacity_gb"] = int(cap.group(1))
    gen = None
    km = _KVR_KSM.match(mpn)
    if km:
        speed_code, lflag, letter = km.groups()
        speed = _KING_SPEED.get(speed_code)
        if speed:
            specs["speed_mhz"] = speed
        gen = _KING_GEN_BY_SPEED.get(speed_code)
        module = low_voltage = None
        if lflag:
            if gen == DDR3 and letter in _KING_FORM:
                module, low_voltage = letter, True  # KVR16LR… = DDR3L + module letter
            elif gen in (DDR4, DDR5):
                module = "L"  # the L *is* the module letter (LRDIMM)
        elif letter in _KING_FORM:
            module = letter
        if module:
            form, ecc = _KING_FORM[module]
            _set_form(specs, form)
            specs["ecc"] = ecc
            if gen == DDR4:
                specs["voltage"] = V12
            elif gen == DDR3:
                specs["voltage"] = V135 if low_voltage else V15
        rank = _KING_RANK.search(mpn, km.end())
        if rank:
            value = f"{_KING_RANK_COUNT[rank.group(1)]}Rx{rank.group(2)}"
            if value in _ALLOWED_RANKS:
                specs["rank"] = value
    if gen:
        specs["ddr_type"] = gen
    else:
        explicit = _KING_GEN.search(mpn)
        if explicit:
            specs["ddr_type"] = {"3": DDR3, "4": DDR4, "5": DDR5}[explicit.group(1)]
    return _r(specs, "Kingston")


# ── Crucial (CT<cap>G<gen><form>F<rank token>…<speed>) ───────────────────
_CRUCIAL = re.compile(r"^CT(\d{1,3})G([345])([A-Z])")
_CRUCIAL_GEN = {"3": DDR3, "4": DDR4, "5": DDR5}
_CRUCIAL_FORM = {
    "R": (RDIMM, True),
    "D": (UDIMM, None),
    "S": (SODIMM, None),
    "W": (SODIMM, None),
    "L": (LRDIMM, True),
}
# The two chars after the form's F are Crucial's explicit rank×width token
# (CT16G4RFD8266 = 2Rx8, CT8G4RFS4266 = 1Rx4, CT64G4LFQ4266 = 4Rx4). No ^ anchor:
# it is applied with Pattern.match(mpn, pos), which anchors at pos already.
_CRUCIAL_RANK = re.compile(r"F([SDQ])([48])")
_CRUCIAL_SPEED = {
    "213": 2133,
    "240": 2400,
    "266": 2666,
    "293": 2933,
    "320": 3200,
    "480": 4800,
    "520": 5200,
    "560": 5600,
    "640": 6400,
}
_CRUCIAL_SPEED_TOKEN = re.compile(r"(\d{3})$")


def _crucial(mpn: str) -> DecodeResult | None:
    m = _CRUCIAL.match(mpn)
    if not m:
        return None
    gen = _CRUCIAL_GEN[m.group(2)]
    specs: dict = {"capacity_gb": int(m.group(1)), "ddr_type": gen}
    if gen == DDR4:
        specs["voltage"] = V12  # DDR3 1.5/1.35 V and DDR5 1.1 V are not safely encoded
    form = _CRUCIAL_FORM.get(m.group(3))
    if form:
        _set_form(specs, form[0])
        if form[1] is True:
            specs["ecc"] = True
        rank = _CRUCIAL_RANK.match(mpn, m.end())
        if rank:
            value = f"{_KING_RANK_COUNT[rank.group(1)]}Rx{rank.group(2)}"
            if value in _ALLOWED_RANKS:
                specs["rank"] = value
    st = _CRUCIAL_SPEED_TOKEN.search(mpn)
    if st and st.group(1) in _CRUCIAL_SPEED:
        specs["speed_mhz"] = _CRUCIAL_SPEED[st.group(1)]
    return _r(specs, "Crucial")


_MEMORY_DECODERS = (_samsung, _hynix, _micron, _kingston, _crucial)


def decode_memory(mpn: str, manufacturer: str | None = None) -> DecodeResult | None:
    """Decode a DRAM-module MPN (already upper-cased) or return None."""
    for decoder in _MEMORY_DECODERS:
        result = decoder(mpn)
        if result is not None and result.specs:
            return result
    return None

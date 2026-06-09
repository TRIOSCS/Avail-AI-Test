"""Deterministic DRAM-module MPN decoders (Samsung, SK Hynix, Micron, Kingston,
Crucial).

CONSERVATIVE: decodes generation / form factor / ECC from the documented module-type codes,
and capacity / speed only where the scheme expresses them cleanly (Kingston `/<cap>`,
Crucial `CT<cap>G…`). Density/speed codes that vary or are ambiguous are skipped, never
guessed. Values map to the seeded `dram` facet keys.
"""

import re

from app.services.mpn_decoder._common import DecodeResult

# Canonical dram enum strings — MUST match commodity_seeds.json exactly.
RDIMM, LRDIMM, UDIMM, SODIMM, DIMM = "RDIMM", "LRDIMM", "UDIMM", "SO-DIMM", "DIMM"
DDR3, DDR4, DDR5 = "DDR3", "DDR4", "DDR5"


def _r(commodity_specs: dict, vendor: str) -> DecodeResult | None:
    return DecodeResult(commodity="dram", vendor=vendor, specs=commodity_specs) if commodity_specs else None


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


def _samsung(mpn: str) -> DecodeResult | None:
    m = _SAMSUNG.match(mpn)
    if not m:
        return None
    mod = _SAMSUNG_MODULE.get(m.group(1))
    if not mod:
        return None
    specs: dict = {"form_factor": mod[0], "ecc": mod[1]}
    if m.group(1) in _SAMSUNG_DDR5_CODES:
        specs["ddr_type"] = DDR5
    elif m.group(2) == "A":
        specs["ddr_type"] = DDR4
    elif m.group(2) == "B":
        specs["ddr_type"] = DDR3
    return _r(specs, "Samsung")


# ── SK Hynix (HM<gen>…<R/U/S/L><6|7>…) ───────────────────────────────────
_HYNIX = re.compile(r"^HM(CG|CT|A|T)")
_HYNIX_GEN = {"T": DDR3, "A": DDR4, "CG": DDR5, "CT": DDR5}
_HYNIX_FF = re.compile(r"[0-9G]([RULS])([678])")
_HYNIX_FORM = {"R": (RDIMM, True), "L": (LRDIMM, True), "U": (UDIMM, None), "S": (SODIMM, None)}


def _hynix(mpn: str) -> DecodeResult | None:
    m = _HYNIX.match(mpn)
    if not m:
        return None
    specs: dict = {"ddr_type": _HYNIX_GEN[m.group(1)]}
    ff = _HYNIX_FF.search(mpn)
    if ff:
        form, ecc = _HYNIX_FORM[ff.group(1)]
        specs["form_factor"] = form
        if ecc is True:
            specs["ecc"] = True  # RDIMM/LRDIMM are always ECC
        elif ff.group(2) == "7":
            specs["ecc"] = True  # 72-bit ECC UDIMM/SODIMM
        elif ff.group(2) == "6":
            specs["ecc"] = False  # 64-bit non-ECC
    return _r(specs, "SK Hynix")


# ── Micron (MTA = DDR4, MTC = DDR5; 72=ECC / 64=non-ECC) ──────────────────
# Only the explicit MTA/MTC MODULE prefixes are decoded. A bare "MT<digit>…" (SDRAM
# components, DDR3/legacy modules) is NOT decoded — never default a generation.
_MICRON = re.compile(r"^MT([AC])\d")
_MICRON_ECC = re.compile(r"(72|64)")


def _micron(mpn: str) -> DecodeResult | None:
    m = _MICRON.match(mpn)
    if not m:
        return None
    specs: dict = {"ddr_type": {"A": DDR4, "C": DDR5}[m.group(1)]}
    ecc = _MICRON_ECC.search(mpn)
    if ecc:
        specs["ecc"] = ecc.group(1) == "72"
    return _r(specs, "Micron")


# ── Kingston (KVR/KSM speed+module; trailing /<cap> across all lines) ─────
_KINGSTON = re.compile(r"^(KVR|KSM|KCP|KTH|KTD|KCS|KF|KSV)")
_KVR_KSM = re.compile(r"^(?:KVR|KSM)(\d{2})([A-Z])")
_KING_CAP = re.compile(r"/(\d{1,3})$")
_KING_GEN = re.compile(r"D([345])")  # explicit DDRx token, e.g. KVR21R15D4/16
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
_KING_FORM = {"N": (UDIMM, False), "E": (UDIMM, True), "R": (RDIMM, True), "L": (LRDIMM, True), "S": (SODIMM, False)}


def _kingston(mpn: str) -> DecodeResult | None:
    if not _KINGSTON.match(mpn):
        return None
    specs: dict = {}
    cap = _KING_CAP.search(mpn)
    if cap:
        specs["capacity_gb"] = int(cap.group(1))
    km = _KVR_KSM.match(mpn)
    if km:
        speed = _KING_SPEED.get(km.group(1))
        if speed:
            specs["speed_mhz"] = speed
        form = _KING_FORM.get(km.group(2))
        if form:
            specs["form_factor"] = form[0]
            specs["ecc"] = form[1]
    gen = _KING_GEN.search(mpn)
    if gen:
        specs["ddr_type"] = {"3": DDR3, "4": DDR4, "5": DDR5}[gen.group(1)]
    return _r(specs, "Kingston")


# ── Crucial (CT<cap>G<gen><form>…<speed>) ────────────────────────────────
_CRUCIAL = re.compile(r"^CT(\d{1,3})G([345])([A-Z])")
_CRUCIAL_GEN = {"3": DDR3, "4": DDR4, "5": DDR5}
_CRUCIAL_FORM = {"R": (RDIMM, True), "D": (UDIMM, None), "S": (SODIMM, None), "W": (SODIMM, None)}
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
    specs: dict = {"capacity_gb": int(m.group(1)), "ddr_type": _CRUCIAL_GEN[m.group(2)]}
    form = _CRUCIAL_FORM.get(m.group(3))
    if form:
        specs["form_factor"] = form[0]
        if form[1] is True:
            specs["ecc"] = True
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

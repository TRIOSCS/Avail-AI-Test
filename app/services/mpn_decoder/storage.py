"""Deterministic HDD MPN decoders (Seagate, Western Digital, Toshiba, HGST/Hitachi).

CONSERVATIVE by design: form_factor comes from the vendor prefix (reliable); capacity is
decoded only where the scheme expresses it unambiguously; usage_class only for well-known
family codes. Anything uncertain is omitted, never guessed. RPM/interface are not reliably
encoded in these MPNs, so they are left to later phases. SSD part-number schemes live in
the sibling ssd.py (WD WDS…, Samsung MZ…, Kioxia, …); Seagate XA/ZA/Nytro remain undecoded.
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
# Modern family-coded scheme: ST<capacityGB><FAMILY><rest>, e.g. ST4000NM0035 (4 TB),
# ST16000NM001G (16 TB), ST1000DM010 (1 TB), ST500LM030 (500 GB). The OLD scheme
# (ST3500418AS) has a digit after the capacity digits, so it never matches this gate.
_SEAGATE = re.compile(r"^ST(\d{3,6})([A-Z]{2})")
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
    m = _SEAGATE.match(mpn)
    if not m:
        return None
    specs: dict = {"capacity_gb": int(m.group(1))}
    fam = _SEAGATE_FAMILY.get(m.group(2))
    if fam:
        specs["form_factor"], specs["usage_class"] = fam
    return DecodeResult(commodity="hdd", vendor="Seagate", specs=specs)


# ── Western Digital (HDD) ────────────────────────────────────────────────
# Modern scheme WD<cap><suffix> where cap is 2-3 digits = TB×10 (WD40 = 4 TB, WD140 = 14 TB).
# The old 4-digit GB scheme (WD5000AAKX) and SSDs (WDS…) don't match: a 4th digit blocks
# [A-Z]+, and WDS starts with a letter.
# form_factor is taken ONLY from a recognized 3.5" family code (every entry in _WD_FAMILY is
# a 3.5" line). The suffix's first letter is NOT a reliable form-factor signal — 2.5" mobile
# drives use varied codes (WD10JPLX, WD…LPLX, WD…LPCX) that don't start "S", so the old
# "S ⇒ 2.5"" rule mislabeled them 3.5". When no family matches we emit capacity only.
_WD = re.compile(r"^WD(\d{2,3})([A-Z]+)")
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
    m = _WD.match(mpn)
    if not m:
        return None
    specs: dict = {"capacity_gb": int(m.group(1)) * 100}  # TB×10 → GB
    suffix = m.group(2)
    for token, uc in _WD_FAMILY:
        if token in suffix:
            specs["usage_class"] = uc
            specs["form_factor"] = FF_35  # every family in _WD_FAMILY is a 3.5" line
            break
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
# families (HUSMM/HUSSL/HUSMR/HUSPR) are Ultrastar SAS *SSDs* — 2.5", not 3.5" — so the
# bare ^HUS gate mislabeled them; they now return None rather than a wrong HDD decode.
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

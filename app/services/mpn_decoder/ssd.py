"""Deterministic SSD MPN decoders (Samsung, Micron, Intel/Solidigm, Kioxia, WD).

What: reads SSD specs (capacity_gb / form_factor / interface, nand_type only where the
      scheme truly encodes it) straight out of standard manufacturer part numbers — no
      network, no LLM. Each vendor decoder is gated by a strict regex for that vendor's
      documented scheme; anything else returns None (never guessed).
Called by: decode_mpn() in app/services/mpn_decoder/__init__.py (worker second pass and
      scripts/decode_mpn_dryrun.py go through it).
Depends on: app.services.mpn_decoder._common only (pure functions).

CONSERVATIVE by design: the seeded `interface` vocabulary has no bare "NVMe" — only
"NVMe PCIe 3.0/4.0/5.0" — so interface is emitted for NVMe drives ONLY when the scheme
pins the PCIe generation (per-family tables); otherwise it is omitted, never guessed.
nand_type is emitted only for Samsung retail families whose product line fixes it
(EVO=TLC, QVO=QLC, …); OEM/enterprise schemes don't encode NAND type, so it's omitted.
"""

import re

from app.services.mpn_decoder._common import DecodeResult

# Canonical enum strings — MUST match the ssd entry in app/data/commodity_seeds.json.
FF_25 = '2.5"'
FF_M2_2280 = "M.2 2280"
FF_M2_2230 = "M.2 2230"
FF_M2_22110 = "M.2 22110"
FF_U2 = "U.2"
FF_U3 = "U.3"
FF_MSATA = "mSATA"
IF_SATA = "SATA"
IF_SAS = "SAS"
IF_NVME3 = "NVMe PCIe 3.0"
IF_NVME4 = "NVMe PCIe 4.0"
TLC, MLC, QLC = "TLC", "MLC", "QLC"

# Samsung/Micron 3-char capacity tokens: <n>T<m> = decimal-TB class sizes with the usual
# 1.92/3.84/7.68 overprovisioned points; "15T"/"30T" = 15.36/30.72 TB; plain digits = GB.
_CAP3 = {
    "1T0": 1000,
    "2T0": 2000,
    "4T0": 4000,
    "8T0": 8000,
    "1T9": 1920,
    "3T8": 3840,
    "7T6": 7680,
    "15T": 15360,
    "30T": 30720,
}


def _cap3(token: str) -> int | None:
    if token in _CAP3:
        return _CAP3[token]
    return int(token) if token.isdigit() else None


# ── Samsung ──────────────────────────────────────────────────────────────
# Two schemes share the position-coded layout MZ[-]<3-char family><3-char capacity><rest>:
#   Retail (dashed):  MZ-V8V1T0B/AM, MZ-77E1T0B/AM, MZ-N6E500BW
#     family char 1: 7 = 2.5" SATA, V = M.2 NVMe (all retail V lines are 2280),
#                    N = M.2 SATA 2280.
#     family chars 2-3 name the product line; for the SATA lines a digit+E/Q means
#     EVO (TLC) / QVO (QLC). The V lines are pinned per family in _SAMSUNG_RETAIL_V
#     (PCIe gen + NAND); unknown V families emit form factor only.
#   OEM (compact):    MZ7LH1T9HMLT, MZVL21T0HCLR, MZQL21T9HCJR, MZNLN256HMHQ,
#                     MZILT3T8HBLS, MZ1LB960HAJQ, MZMPC032HBCD
#     family char 1: 7 = 2.5" SATA, N = M.2 SATA 2280, M = mSATA, V = M.2 NVMe 2280,
#                    Q = U.2 NVMe, I = 2.5" SAS (PM1633/1643/1653), 1 = M.2 22110 NVMe.
#     PCIe generation only via the per-family tables (PM9A1/PM9A3 = gen4; PM951/961/
#     981/983/991 = gen3); unknown families emit form factor only — never a guessed gen.
_SAMSUNG_RETAIL = re.compile(r"^MZ-([7VN][A-Z0-9]{2})(\d[0-9A-Z]{2})")
_SAMSUNG_OEM = re.compile(r"^MZ([7NMVQI1][A-Z0-9]{2})(\d[0-9A-Z]{2})")
_SAMSUNG_RETAIL_V = {  # family -> (interface, nand): 960/970/980/990 retail M.2 lines
    "V6E": (IF_NVME3, TLC),  # 960 EVO
    "V6P": (IF_NVME3, MLC),  # 960 PRO
    "V7E": (IF_NVME3, TLC),  # 970 EVO
    "V7S": (IF_NVME3, TLC),  # 970 EVO Plus
    "V7P": (IF_NVME3, MLC),  # 970 PRO
    "V8V": (IF_NVME3, TLC),  # 980
    "V8P": (IF_NVME4, TLC),  # 980 PRO
    "V9P": (IF_NVME4, TLC),  # 990 PRO
}
_SAMSUNG_OEM_GEN = {  # OEM family -> pinned PCIe generation
    "VL2": IF_NVME4,  # PM9A1
    "VLB": IF_NVME3,  # PM981/981a
    "VLW": IF_NVME3,  # PM961
    "VLQ": IF_NVME3,  # PM991/991a (2280 form)
    "VLV": IF_NVME3,  # PM951
    "QL2": IF_NVME4,  # PM9A3 U.2
    "QLB": IF_NVME3,  # PM983 U.2
    "QLW": IF_NVME3,  # PM963 U.2
    "1L2": IF_NVME4,  # PM9A3 M.2 22110
    "1LB": IF_NVME3,  # PM983 M.2 22110
}
_SAMSUNG_OEM_FORM = {  # family char 1 -> (form_factor, interface-or-None)
    "7": (FF_25, IF_SATA),
    "N": (FF_M2_2280, IF_SATA),
    "M": (FF_MSATA, IF_SATA),
    "V": (FF_M2_2280, None),  # NVMe; gen only via _SAMSUNG_OEM_GEN
    "Q": (FF_U2, None),
    "I": (FF_25, IF_SAS),
    "1": (FF_M2_22110, None),
}
_SAMSUNG_RETAIL_SATA_FORM = {  # retail family char 1 -> form factor (both lines are SATA)
    "7": FF_25,  # 2.5" SATA (e.g. 870 EVO/QVO)
    "N": FF_M2_2280,  # M.2 SATA 2280 (e.g. 860 EVO M.2)
}


def _samsung_retail_nand(family: str) -> str | None:
    # digit-generation + E/Q means EVO (TLC) / QVO (QLC); letters like 7KE (850 PRO, MLC)
    # deliberately do NOT match — omitted rather than guessed.
    if family[1].isdigit():
        return {"E": TLC, "Q": QLC}.get(family[2])
    return None


def _samsung(mpn: str) -> DecodeResult | None:
    m = _SAMSUNG_RETAIL.match(mpn)
    if m:
        family = m.group(1)
        specs: dict = {}
        cap = _cap3(m.group(2))
        if cap:
            specs["capacity_gb"] = cap
        sata_form = _SAMSUNG_RETAIL_SATA_FORM.get(family[0])
        if sata_form:  # 7 = 2.5" SATA, N = M.2 SATA 2280
            specs["form_factor"], specs["interface"] = sata_form, IF_SATA
            nand = _samsung_retail_nand(family)
            if nand:
                specs["nand_type"] = nand
        else:  # V — retail M.2 NVMe, all 2280
            specs["form_factor"] = FF_M2_2280
            known = _SAMSUNG_RETAIL_V.get(family)
            if known:
                specs["interface"], specs["nand_type"] = known
        return DecodeResult(commodity="ssd", vendor="Samsung", specs=specs) if specs else None

    m = _SAMSUNG_OEM.match(mpn)
    if not m:
        return None
    family = m.group(1)
    form, interface = _SAMSUNG_OEM_FORM[family[0]]
    specs = {"form_factor": form}
    if interface:
        specs["interface"] = interface
    else:
        gen = _SAMSUNG_OEM_GEN.get(family)
        if gen:
            specs["interface"] = gen
    cap = _cap3(m.group(2))
    if cap:
        specs["capacity_gb"] = cap
    return DecodeResult(commodity="ssd", vendor="Samsung", specs=specs)


# ── Micron (MTFD<3-letter code><capacity token><family suffix>) ──────────
# Code letters after MTFD: char 1 = interface (D = SATA, K = NVMe PCIe gen4 era —
# 7400/7450/9400/6500/2400/3500, H = NVMe PCIe gen3 era — 7300/9300/2200/2300);
# chars 2-3 = form factor. Only codes verified against shipping product lines decode;
# an unknown code returns None (no partial guess). Examples: MTFDDAK1T9TDS (5300 PRO
# 2.5" SATA), MTFDDAV240TCB (1100 M.2 SATA), MTFDKBA960TFR (7450 PRO M.2 2280),
# MTFDKCB3T8TDZ (7400 PRO U.3 7mm), MTFDKCC15T3TFR (7450 PRO U.3 15mm),
# MTFDHAL3T8TDP (9300 PRO U.2).
_MICRON_SSD = re.compile(r"^MTFD([A-Z]{3})(\d[0-9A-Z]{2})")
_MICRON_SSD_CODES = {
    "DAK": (FF_25, IF_SATA),
    "DAV": (FF_M2_2280, IF_SATA),
    "KBA": (FF_M2_2280, IF_NVME4),
    "KBG": (FF_M2_22110, IF_NVME4),
    "KBK": (FF_M2_2230, IF_NVME4),  # 2400 client line
    "KCB": (FF_U3, IF_NVME4),  # U.3 7mm
    "KCC": (FF_U3, IF_NVME4),  # U.3 15mm
    "HBA": (FF_M2_2280, IF_NVME3),
    "HAL": (FF_U2, IF_NVME3),
}


def _micron_ssd(mpn: str) -> DecodeResult | None:
    m = _MICRON_SSD.match(mpn)
    if not m:
        return None
    code = _MICRON_SSD_CODES.get(m.group(1))
    if code is None:
        return None  # unrecognized code — scheme membership unconfirmed, decode nothing
    specs: dict = {"form_factor": code[0], "interface": code[1]}
    cap = _cap3(m.group(2))
    if cap:
        specs["capacity_gb"] = cap
    return DecodeResult(commodity="ssd", vendor="Micron", specs=specs)


# ── Intel / Solidigm (SSD<scheme code>…<3-digit capacity><G|T><gen suffix>) ─
# Scheme code (chars 4-6): SC2 = 2.5" SATA, SCK = M.2 SATA 2280, PE2 = U.2 NVMe PCIe 3.0
# (P4500/P4510/P4610, Optane DC), PEK = M.2 NVMe PCIe 3.0 2280 (660p/670p/760p),
# PF2 = U.2 NVMe PCIe 4.0 (D7-P5510/P5520, D5-P5316). E/F after P is Intel's PCIe-gen
# letter (3.0/4.0). Capacity token: <n>G = n GB (SSDSC2KB960G8 → 960); <n>T = decimal-TB
# table (SSDPE2KX040T8 → 4000, SSDSC2KB019T8 → 1920). Other prefixes (SSDPEL M.2 22110,
# SSDPFK client gen4, mSATA SSDM…) are NOT decoded — out of verified scope.
_INTEL = re.compile(r"^SSD(SC2|SCK|PE2|PEK|PF2)")
_INTEL_CODES = {
    "SC2": (FF_25, IF_SATA),
    "SCK": (FF_M2_2280, IF_SATA),
    "PE2": (FF_U2, IF_NVME3),
    "PEK": (FF_M2_2280, IF_NVME3),
    "PF2": (FF_U2, IF_NVME4),
}
_INTEL_CAP = re.compile(r"(\d{3})([GT])")
_INTEL_TB = {  # 3-digit decimal-TB tokens -> GB
    "010": 1000,
    "015": 1500,
    "016": 1600,
    "019": 1920,
    "020": 2000,
    "032": 3200,
    "038": 3840,
    "040": 4000,
    "064": 6400,
    "076": 7680,
    "080": 8000,
    "153": 15360,
    "307": 30720,
    "614": 61440,
}


def _intel(mpn: str) -> DecodeResult | None:
    m = _INTEL.match(mpn)
    if not m:
        return None
    form, interface = _INTEL_CODES[m.group(1)]
    specs: dict = {"form_factor": form, "interface": interface}
    cap = _INTEL_CAP.search(mpn, m.end())
    if cap:
        if cap.group(2) == "G":
            specs["capacity_gb"] = int(cap.group(1))
        elif cap.group(1) in _INTEL_TB:
            specs["capacity_gb"] = _INTEL_TB[cap.group(1)]
    return DecodeResult(commodity="ssd", vendor="Intel", specs=specs)


# ── Kioxia (ex-Toshiba memory) ───────────────────────────────────────────
# KXG<gen> = XG client M.2 2280 NVMe (XG5/XG6 = PCIe 3.0, XG7/XG8 = PCIe 4.0).
# KPM<gen> = PM enterprise SAS 2.5" (PM5/PM6/PM7 — all 2.5" SAS).
# KCM<gen>/KCD<gen> = CM/CD enterprise NVMe: gen 5 = U.2 PCIe 3.0, gen 6 = U.3 PCIe 4.0;
# later gens (CM7+: U.3/E3.S variants) decode capacity only — form/gen not pinned here.
# Capacity tokens: <n>G = n GB; enterprise <x>T<yz> = x.yz TB (1T92 → 1920); client
# XG tokens are binary (KXG60ZNV1T02 = 1024 GB) — only the verified tokens decode.
_KIOXIA_KXG = re.compile(r"^KXG(\d)")
_KIOXIA_KXG_GEN = {"5": IF_NVME3, "6": IF_NVME3, "7": IF_NVME4, "8": IF_NVME4}
_KIOXIA_KXG_CAP = re.compile(r"(1T02|2T04|4T08|\d{3,4}G)")
_KIOXIA_KXG_TB = {"1T02": 1024, "2T04": 2048, "4T08": 4096}
_KIOXIA_ENT = re.compile(r"^K(PM|CM|CD)(\d)")
_KIOXIA_ENT_NVME = {"5": (FF_U2, IF_NVME3), "6": (FF_U3, IF_NVME4)}
_KIOXIA_ENT_CAP = re.compile(r"(1T92|3T84|7T68|15T3|30T7|\d{3}G)")
_KIOXIA_ENT_TB = {"1T92": 1920, "3T84": 3840, "7T68": 7680, "15T3": 15360, "30T7": 30720}


def _kioxia_cap(token: str, tb_table: dict[str, int]) -> int:
    # Enterprise <x>T<yz> / client xT0z tokens map via the table; a plain <n>G token
    # drops its trailing G and reads the leading digits as GB.
    return tb_table[token] if token in tb_table else int(token[:-1])


def _kioxia(mpn: str) -> DecodeResult | None:
    m = _KIOXIA_KXG.match(mpn)
    if m:
        specs: dict = {"form_factor": FF_M2_2280}  # every XG generation is M.2 2280
        gen = _KIOXIA_KXG_GEN.get(m.group(1))
        if gen:
            specs["interface"] = gen
        cap = _KIOXIA_KXG_CAP.search(mpn, m.end())
        if cap:
            specs["capacity_gb"] = _kioxia_cap(cap.group(1), _KIOXIA_KXG_TB)
        return DecodeResult(commodity="ssd", vendor="Kioxia", specs=specs)

    m = _KIOXIA_ENT.match(mpn)
    if not m:
        return None
    specs = {}
    if m.group(1) == "PM":
        specs["form_factor"], specs["interface"] = FF_25, IF_SAS
    else:  # CM / CD NVMe
        known = _KIOXIA_ENT_NVME.get(m.group(2))
        if known:
            specs["form_factor"], specs["interface"] = known
    cap = _KIOXIA_ENT_CAP.search(mpn, m.end())
    if cap:
        specs["capacity_gb"] = _kioxia_cap(cap.group(1), _KIOXIA_ENT_TB)
    return DecodeResult(commodity="ssd", vendor="Kioxia", specs=specs) if specs else None


# ── Western Digital SSD (WDS<3 digits><G|T><gen digit><suffix>) ──────────
# Capacity: <n>G = n GB, <n>T = n/100 TB (WDS100T… = 1 TB → 1000). The trailing
# letter-0-letter suffix pins form factor + interface per the Blue/Black/Red/Green
# retail conventions; the digit before it is a product revision and is ignored.
# Unknown suffixes emit capacity only. Anchored ^WDS so WD HDDs (WD40EFRX…) and the
# storage decoder's WD gate (^WD\d) never collide.
_WD_SSD = re.compile(r"^WDS(\d{3})([GT])(\d)([A-Z]0[A-Z])")
_WD_SSD_SUFFIX = {
    "B0A": (FF_25, IF_SATA),  # Blue 2.5" SATA
    "R0A": (FF_25, IF_SATA),  # Red SA500 2.5"
    "G0A": (FF_25, IF_SATA),  # Green 2.5"
    "B0B": (FF_M2_2280, IF_SATA),  # Blue M.2 SATA
    "G0B": (FF_M2_2280, IF_SATA),  # Green M.2 SATA
    "B0C": (FF_M2_2280, IF_NVME3),  # Blue SN500/SN550/SN570
    "X0C": (FF_M2_2280, IF_NVME3),  # Black SN750
    "R0C": (FF_M2_2280, IF_NVME3),  # Red SN700
    "X0E": (FF_M2_2280, IF_NVME4),  # Black SN770/SN850/SN850X
}


def _wd_ssd(mpn: str) -> DecodeResult | None:
    m = _WD_SSD.match(mpn)
    if not m:
        return None
    gb = int(m.group(1))
    specs: dict = {"capacity_gb": gb * 10 if m.group(2) == "T" else gb}
    known = _WD_SSD_SUFFIX.get(m.group(4))
    if known:
        specs["form_factor"], specs["interface"] = known
    return DecodeResult(commodity="ssd", vendor="Western Digital", specs=specs)


_SSD_DECODERS = (_samsung, _micron_ssd, _intel, _kioxia, _wd_ssd)


def decode_ssd(mpn: str, manufacturer: str | None = None) -> DecodeResult | None:
    """Decode an SSD MPN (already upper-cased) or return None."""
    for decoder in _SSD_DECODERS:
        result = decoder(mpn)
        if result is not None and result.specs:
            return result
    return None

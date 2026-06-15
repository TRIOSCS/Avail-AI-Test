"""Classify an MPN as an OEM/system-vendor FRU/spare/service part number.

Pure, regex-based vendor detection used to gate the OEM enrichment tiers (cross-ref +
OEM official description) in ``enrich_card``. Returns the likely OEM vendor or ``None``.
The label is advisory only — it seeds the search prompt; correctness is enforced
downstream by the Python gates in ``oem_extractor`` / ``enrich_card``, never here.

Called by: app.services.authoritative_enrichment_service.enrich_card,
scripts.backfill_oem_enrichment. Depends on: stdlib ``re`` only.
"""

from __future__ import annotations

import re

# Vendors whose patterns are precise enough that an OEM-tier miss means "genuinely an
# uncatalogued OEM service part" (-> not_catalogued, 30-day backoff). The broad Dell
# 5-char pattern is excluded: a miss there is more likely a generic part, so it stays
# not_found (22h retry) instead of being parked for a month.
HIGH_PRECISION_VENDORS: frozenset[str] = frozenset({"lenovo", "ibm", "hpe", "acer", "asus"})

# HP/HPE OPTION KIT shape (819203-B21) — named because oem_crosswalk_enrich gates the
# oem_sourced status uplift on it: unlike service spares (\d{6}-\d{3}), option kits ARE
# widely distributor-catalogued, so an unenriched -B21 card keeps its free tier-90
# connector chance before taking the tier-80 crosswalk status.
OPTION_KIT_RE: re.Pattern[str] = re.compile(r"^\d{6}-B\d{2}$")

# Ordered (priority) (vendor, pattern). First match wins. Anchored, matched against the
# UPPERCASED stripped display_mpn. Each pattern is justified by a real not_found sample
# (see spec §1).
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Lenovo modern FRU/option: 5x + 2 digits + letter + 5 digits (5B20L64949, 5T10Q96500)
    ("lenovo", re.compile(r"^5[A-Z]\d{2}[A-Z]\d{5}$")),
    # Lenovo/IBM classic FRU: 2 digits + letter + 4 alnum (38L7669, 46C9040)
    ("lenovo", re.compile(r"^\d{2}[A-Z][A-Z0-9]{4}$")),
    # Lenovo/IBM 7-char FRU: 00/01 + 5 alnum (01HW917, 00E2891, 01LV731)
    ("lenovo", re.compile(r"^0[01][A-Z0-9]{5}$")),
    # Acer dotted part code: 2 alnum . 5 alnum . 3 alnum (NB.MBC11.003, 33.G55N7.002)
    ("acer", re.compile(r"^[A-Z0-9]{2}\.[A-Z0-9]{5}\.[A-Z0-9]{3}$")),
    # ASUS module code: 2 digits NB + 4 alnum - tail (60NB0690-MB1820)
    ("asus", re.compile(r"^\d{2}NB[A-Z0-9]{4}-[A-Z0-9]+$")),
    # ASUS 0X###-######## (0B200-00930000)
    ("asus", re.compile(r"^0[A-Z]\d{3}-\d{8}$")),
    # HP/HPE spare: 6 digits - 3 digits (918042-601, 619559-001)
    ("hpe", re.compile(r"^\d{6}-\d{3}$")),
    # HP/HPE option kit: 6 digits - B + 2 digits (819203-B21, 875942-B21)
    ("hpe", OPTION_KIT_RE),
    # HP/HPE L-series spare: L + 5 digits - 3 digits (L15335-001)
    ("hpe", re.compile(r"^L\d{5}-\d{3}$")),
    # EMC 303-x assembly/spare: 303 - 3 digits - 3 digits + optional rev letter
    # (303-104-000D, 303-081-103B). The measured bulk of the web tier's
    # "no trusted source" rejects — EMC PNs surface only on reseller pages, so the
    # shape gates the OEM-FRU web-extract skip (enrichment_skip_web_for_oem_mpns).
    # NOT in HIGH_PRECISION_VENDORS: an OEM-tier miss stays not_found (22h retry),
    # not parked not_catalogued for 30 days.
    ("emc", re.compile(r"^303-\d{3}-\d{3}[A-Z]?$")),
    # Dell 5-char spare with >=1 letter (HV52W, 66YYK). Broad/low-priority; a false
    # positive costs only a wasted web call (genuine MPNs resolve at earlier tiers first).
    ("dell", re.compile(r"^(?=[A-Z0-9]{5}$)[A-Z0-9]*[A-Z][A-Z0-9]*$")),
]


def classify_oem_vendor(display_mpn: str | None) -> str | None:
    """Return the likely OEM vendor for an OEM/FRU/spare code, or ``None``.

    Never raises on empty/malformed input (returns ``None``). The vendor label only seeds
    the cross-ref / description search prompt; the Python trust gates downstream enforce
    correctness.
    """
    if not isinstance(display_mpn, str):
        return None
    mpn = display_mpn.strip().upper()
    if not mpn:
        return None
    for vendor, pat in _PATTERNS:
        if pat.match(mpn):
            return vendor
    return None

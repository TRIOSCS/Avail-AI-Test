"""Verified MPN-prefix → commodity rules + the real-CPU guard for the cpu-bucket
cleanup.

Each PREFIX_RULES entry was verified against live `category='cpu'` sample MPNs; the
commodity is a canonical key (asserted by test_every_prefix_rule_targets_valid_vocab).
PRECISION FIRST: anchored sub-prefixes only (never a bare ambiguous letter), and
CPU_GUARD is checked BEFORE the rules so a real Intel/AMD identifier is never re-homed
(e.g. Intel CD80… must not hit the TI CD74 logic rule). Called by: classifier.py.
"""

from __future__ import annotations

import re

# (anchored regex on the UPPERCASED MPN, canonical commodity key). First match wins.
PREFIX_RULES: list[tuple[re.Pattern[str], str]] = [
    # TE Connectivity: a 7-digit core + single trailing digit (NNNNNNN-N or N-NNNNNNN-N).
    # The 7-digit core is the discriminator from HP CPU spares (6-digit core + 3-digit suffix,
    # e.g. 726719-001) — those MUST stay in `cpu` (OEM-spare cohort), never become connectors.
    (re.compile(r"^([0-9]-)?[0-9]{7}-[0-9]"), "connectors"),
    (re.compile(r"^(SSW|CLT|CLP|SMM|SSM|SLW|TSW|HLE|FLE|BSW)-"), "connectors"),  # Samtec series
    (re.compile(r"^NRWA"), "capacitors"),  # Nichicon Al electrolytic
    (re.compile(r"^TAJ"), "capacitors"),  # AVX/Kyocera tantalum
    (re.compile(r"^B32"), "capacitors"),  # EPCOS/TDK film cap
    (re.compile(r"^BLM"), "inductors"),  # Murata ferrite bead
    (re.compile(r"^CRCW"), "resistors"),  # Vishay thick-film resistor
    (re.compile(r"^CD74"), "logic_ic"),  # TI CD74HC logic
    (re.compile(r"^(SN74|74[A-Z])"), "logic_ic"),  # 74-series logic
    (re.compile(r"^BCM[0-9]"), "logic_ic"),  # Broadcom
]

# MPNs that ARE real CPUs — never reclassify (defense-in-depth, checked before PREFIX_RULES).
CPU_GUARD: list[re.Pattern[str]] = [
    re.compile(r"^S[RL][0-9A-Z]{2,4}$"),  # Intel sSpec (SR3QS, SL5CH)
    re.compile(r"^(BX80|CM80|CD80|AT80|FC80|FH80|HH80|CW80)"),  # Intel ordering codes
    re.compile(
        r"(XEON|CORE\s?I[3579]|PENTIUM|CELERON|"
        r"GOLD\s?[0-9]|SILVER\s?[0-9]|PLATINUM\s?[0-9]|BRONZE\s?[0-9]|^E[357]-[0-9])"
    ),  # Intel model strings
    re.compile(r"(EPYC|RYZEN|OPTERON|ATHLON|THREADRIPPER)"),  # AMD model words
    re.compile(r"^10[0-9]-[0-9]{9}$"),  # AMD OPN (100-000000053)
]

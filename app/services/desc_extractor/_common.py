"""Shared types + constants for the deterministic description→spec extractors.

What: DescResult dataclass, the canonical SpecDict specs type every extractor
      returns, plus the source tag / confidence every desc-parsed spec is written
      with (see record_spec).
Called by: app/services/desc_extractor/{__init__,storage,memory,power,display,
      tape,gpu,board,cpu,writer}.py.
Depends on: nothing (pure).
"""

import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import TypeVar

_T = TypeVar("_T")

# NAND-die context (re-audit 2026-06-10, residual class 3 — card 74115): NAND component
# descriptions write their die density under the gigaBIT convention with a BARE "G"
# ("Nand, 512G, MLC" = 512 Gbit = 64 GB), which the round-1 Gb-guard (lowercase-b
# _BIT_UNITS rewrite in __init__.py) cannot see. Signals, on the upper-cased text:
# DIE-SPECIFIC only — the NAND word itself or a Micron MT29-series die MPN echoed
# into the description. Cell-level tokens (SLC/MLC/TLC/QLC) and standalone x8/x16
# organization tokens are deliberately NOT signals: flash-type tokens appear on
# ordinary SSD listings where a bare "<n>G" IS gigabytes of drive capacity
# ("SSD, 480G, TLC, SATA"), and spaced "X8" collides with PCIe lane widths
# ("PCIE X8") and spaced DRAM rank/org tokens ("2R X8") — treating those as die
# context suppressed real capacities corpus-wide. The audit's actual die cards
# carry the NAND word and/or the MT29 MPN, so the die-specific gate still catches
# them while real drive/module descriptions keep extracting.
_NAND_DIE_CONTEXT = re.compile(r"\bNAND\b|\bMT29[A-Z0-9]")


def nand_die_context(text: str) -> bool:
    """True when the upper-cased description carries die-specific NAND signals (the NAND
    word or an MT29-series die MPN) — bare ``<n>G`` tokens then denote gigaBITS of die
    density, never gigabytes of capacity, so the capacity extractors must skip them
    (deliberate NO-WRITE: densities are not a seeded spec and are never ÷8-converted — a
    wrong facet value is worse than a missing one)."""
    return bool(_NAND_DIE_CONTEXT.search(text))


def unique_or_none(values: AbstractSet[_T]) -> _T | None:
    """The single member of *values*, or None when it is empty or holds a conflict.

    The shared "unique-or-omit" rule every extractor applies: a facet is emitted only
    when exactly one candidate survives, so conflicting signals omit the key rather than
    guess (a wrong facet value is worse than a missing one).
    """
    return next(iter(values)) if len(values) == 1 else None


# Canonical specs mapping — EVERY per-commodity extractor returns exactly this type.
# dict is invariant in its value type, so a module returning a narrower union (e.g.
# dict[str, str]) would fail the extract_desc dispatch under mypy; the shared alias
# keeps all seven extractors and DescResult.specs in lockstep.
SpecDict = dict[str, str | int | float | bool]

# Source tag + confidence for everything this extractor writes (see record_spec).
DESC_SOURCE = "desc_parse"
DESC_CONFIDENCE = 0.90  # deterministic token grammar. Arbitration is by the F1 tier
# ladder (spec_tiers.SOURCE_TIER: desc_parse=83, between mpn_decode 85 and the AI
# spec reader 60) — record_spec rejects any write that loses the ladder, so this
# confidence is provenance metadata, not the cross-source conflict rule.

# OEM-authoritative description: the same desc grammar run over HP/HPE's OWN verbatim
# PartSurfer catalog text (fetched live), not the card's own desc. Outranks desc_parse
# (spec_tiers.SOURCE_TIER: partsurfer_desc=84 > desc_parse 83).
PARTSURFER_DESC_SOURCE = "partsurfer_desc"
PARTSURFER_DESC_CONFIDENCE = 0.90

# Distributor-connector description: the same desc grammar run over a DigiKey/Mouser/
# element14/OEMSecrets/Nexar product description we ALREADY fetch (not the card's own).
# Outranks desc_parse (spec_tiers.SOURCE_TIER: connector_desc=84 > desc_parse 83).
CONNECTOR_DESC_SOURCE = "connector_desc"
CONNECTOR_DESC_CONFIDENCE = 0.90

# The only commodities the extractor fills specs for — single source of truth shared
# by extract_desc (routing) and writer.py (card eligibility / the spec'd _HANDLED
# set). The PSU-vs-CPU wattage guard is structural: only extract_psu can emit the
# `wattage` key, while the cpu route emits `tdp_watts` — CPU "135W" TDP text can
# never land in wattage and PSU ratings can never land in tdp_watts. Passive
# commodities (capacitors, resistors) join via the same distributor-description grammar.
SPEC_COMMODITIES = frozenset(
    {
        "hdd",
        "ssd",
        "dram",
        "power_supplies",
        "displays",
        "tape_drives",
        "gpu",
        "motherboards",
        "cpu",
        "capacitors",
        "resistors",
    }
)


@dataclass
class DescResult:
    """Outcome of extracting one description string.

    ``commodity`` is the inferred commodity KEY hint (e.g. "hdd", "ssd", "dram",
    "power_supplies", "displays", "tape_drives", "gpu", "motherboards", "cpu") for
    CALLERS to use — the extractor and its writer never set a card's category from
    it. It is always set: when no commodity can be established, extract_desc
    returns None instead of a DescResult.
    ``specs`` maps seeded spec_key -> value (enum string / int / float / bool — bool
    only for boolean schemas like dram.ecc, exactly like mpn_decoder.DecodeResult).
    """

    commodity: str
    specs: SpecDict = field(default_factory=dict)
    confidence: float = DESC_CONFIDENCE

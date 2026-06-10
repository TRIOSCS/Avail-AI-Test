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
# the NAND word itself, a cell-level token (SLC/MLC/TLC/QLC), a Micron MT29-series
# die MPN echoed into the description, or a standalone x8/x16 organization token
# (the \b guards keep DRAM rank tokens like "2RX8" and org strings like "256MX16"
# from matching — no boundary between the letter and the X).
_NAND_DIE_CONTEXT = re.compile(r"\bNAND\b|\b[SMTQ]LC\b|\bMT29[A-Z0-9]|\bX0?8\b|\bX16\b")


def nand_die_context(text: str) -> bool:
    """True when the upper-cased description carries NAND-die signals — bare ``<n>G``
    tokens then denote gigaBITS of die density, never gigabytes of capacity, so the
    capacity extractors must skip them (deliberate NO-WRITE: densities are not a seeded
    spec and are never ÷8-converted — a wrong facet value is worse than a missing
    one)."""
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

# The only commodities the extractor fills specs for — single source of truth shared
# by extract_desc (routing) and writer.py (card eligibility / the spec'd _HANDLED
# set). The PSU-vs-CPU wattage guard is structural: only extract_psu can emit the
# `wattage` key, while the cpu route emits `tdp_watts` — CPU "135W" TDP text can
# never land in wattage and PSU ratings can never land in tdp_watts.
SPEC_COMMODITIES = frozenset(
    {"hdd", "ssd", "dram", "power_supplies", "displays", "tape_drives", "gpu", "motherboards", "cpu"}
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

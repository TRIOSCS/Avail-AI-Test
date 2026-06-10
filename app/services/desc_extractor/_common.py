"""Shared types + constants for the deterministic description→spec extractors.

What: DescResult dataclass plus the source tag / confidence every desc-parsed spec is
      written with (see record_spec).
Called by: app/services/desc_extractor/{__init__,storage,memory,writer}.py.
Depends on: nothing (pure).
"""

from dataclasses import dataclass, field

# Source tag + confidence for everything this extractor writes (see record_spec).
DESC_SOURCE = "desc_parse"
DESC_CONFIDENCE = 0.90  # deterministic token grammar — sits between mpn_decode (0.95)
# and the AI spec reader (0.85); record_spec never lets it overwrite a protected
# vendor-API value, and the writer skips keys already held at higher confidence.

# The only commodities the extractor fills specs for — single source of truth shared
# by extract_desc (routing) and writer.py (card eligibility / the spec'd _HANDLED
# set). cpu stays hint-only (empty specs): keeping it OUT of this set is the
# PSU-vs-CPU wattage guard — a `wattage` key can only ever be emitted on the
# power_supplies route, so CPU "135W" TDP text is structurally unreachable.
SPEC_COMMODITIES = frozenset({"hdd", "ssd", "dram", "power_supplies", "displays", "tape_drives", "gpu", "motherboards"})


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
    specs: dict[str, str | int | float | bool] = field(default_factory=dict)
    confidence: float = DESC_CONFIDENCE

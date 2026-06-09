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


@dataclass
class DescResult:
    """Outcome of extracting one description string.

    ``commodity`` is an inferred commodity KEY hint (e.g. "hdd", "ssd", "dram",
    "motherboards", "power_supplies", "cpu") for CALLERS to use — the extractor and
    its writer never set a card's category from it.
    ``specs`` maps seeded spec_key -> value (enum string / int / float / bool — bool
    only for boolean schemas like dram.ecc, exactly like mpn_decoder.DecodeResult).
    """

    commodity: str | None
    specs: dict[str, str | int | float | bool] = field(default_factory=dict)
    confidence: float = DESC_CONFIDENCE

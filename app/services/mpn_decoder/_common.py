"""Shared types + helpers for the MPN decoders."""

from dataclasses import dataclass, field

# Source tag + confidence for everything these decoders write (see record_spec).
DECODE_SOURCE = "mpn_decode"
DECODE_CONFIDENCE = 0.95  # deterministic rule. The F1 tier ladder (app/services/spec_tiers.py)
# maps mpn_decode to tier 85, so it outranks AI description mining (spec_extraction, tier 60)
# regardless of write order, and never overwrites a vendor-API (tier 90) or manual (100) value.


@dataclass
class DecodeResult:
    """Outcome of decoding one MPN.

    `commodity` is the canonical material_cards.category key (e.g. "hdd", "ssd", "dram").
    `specs` maps seeded spec_key -> value (enum string / int / float / "true"/"false").
    """

    commodity: str
    vendor: str
    specs: dict[str, str | int | float | bool] = field(default_factory=dict)
    confidence: float = DECODE_CONFIDENCE

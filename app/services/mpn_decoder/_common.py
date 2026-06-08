"""Shared types + helpers for the MPN decoders."""

from dataclasses import dataclass, field

# Source tag + confidence for everything these decoders write (see record_spec).
DECODE_SOURCE = "mpn_decode"
DECODE_CONFIDENCE = 0.95  # deterministic rule — outranks AI description mining (0.85),
# but record_spec never lets it overwrite a protected vendor-API value.


@dataclass
class DecodeResult:
    """Outcome of decoding one MPN.

    `commodity` is the canonical material_cards.category key (e.g. "hdd", "ssd", "dram").
    `specs` maps seeded spec_key -> value (enum string / int / float / "true"/"false").
    """

    commodity: str
    vendor: str
    specs: dict[str, str | int | float] = field(default_factory=dict)
    confidence: float = DECODE_CONFIDENCE

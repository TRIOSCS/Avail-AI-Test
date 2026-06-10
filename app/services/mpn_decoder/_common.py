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
    `dropped` maps spec_key -> value the decoder REFUSED to emit because the value
    failed a plausibility gate (today: hdd capacity off the shipped-capacity grid in
    storage.decode_storage). Kept out of `specs` so no caller can persist it, but
    carried on the result so writer.py can surface the drop in its aggregate
    drop-WARNING — a plausibility rejection must never be silent.
    """

    commodity: str
    vendor: str
    specs: dict[str, str | int | float | bool] = field(default_factory=dict)
    confidence: float = DECODE_CONFIDENCE
    dropped: dict[str, str | int | float | bool] = field(default_factory=dict)

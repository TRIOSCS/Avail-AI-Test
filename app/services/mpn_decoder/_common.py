"""Shared types + helpers for the MPN decoders."""

from dataclasses import dataclass, field

# Source tag + confidence for everything these decoders write (see record_spec).
DECODE_SOURCE = "mpn_decode"
DECODE_CONFIDENCE = 0.95  # deterministic rule. The F1 tier ladder (app/services/spec_tiers.py)
# maps mpn_decode to tier 85, so it outranks AI description mining (spec_extraction, tier 60)
# regardless of write order, and never overwrites a vendor-API (tier 90) or manual (100) value.

# Reason tags for DecodeResult.drop_reasons — writer.py keys its aggregate drop-WARNING
# counters off these, so an over-tight grid and an over-tight envelope stay distinguishable.
DROP_OFF_GRID = "off_grid"  # hdd capacity off storage.HDD_SHIPPED_CAPACITY_GB
DROP_OUT_OF_ENVELOPE = "out_of_envelope"  # Seagate capacity outside _SEAGATE_ENVELOPE / unlisted family


@dataclass
class DecodeResult:
    """Outcome of decoding one MPN.

    `commodity` is the canonical material_cards.category key (e.g. "hdd", "ssd", "dram").
    `specs` maps seeded spec_key -> value (enum string / int / float / "true"/"false").
    `dropped` maps spec_key -> value the decoder REFUSED to emit because the value
    failed a plausibility gate (hdd capacity off the shipped-capacity grid, or a modern
    Seagate capacity outside its family envelope — see storage.py). Kept out of `specs`
    so no caller can persist it, but carried on the result so writer.py can surface the
    drop in its aggregate drop-WARNING — a plausibility rejection must never be silent.
    `specs` MAY be empty while `dropped` is populated (every decoded value failed its
    gate): decode_mpn still returns such a result so the drop stays observable; every
    write path must check `specs` (not result-is-None) before persisting anything.
    `drop_reasons` maps each dropped spec_key -> its DROP_* reason tag.
    """

    commodity: str
    vendor: str
    specs: dict[str, str | int | float | bool] = field(default_factory=dict)
    confidence: float = DECODE_CONFIDENCE
    dropped: dict[str, str | int | float | bool] = field(default_factory=dict)
    drop_reasons: dict[str, str] = field(default_factory=dict)

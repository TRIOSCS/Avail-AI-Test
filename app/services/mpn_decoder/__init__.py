"""Deterministic MPN → spec decoders (Phase 1 of MPN-decode enrichment).

What: reads parametric specs straight out of a *standard manufacturer* part number
      (drives + DRAM modules) with NO network and NO LLM — zero hallucination. Each vendor
      decoder is gated by a strict regex so only recognized schemes decode; anything else
      returns None (never guessed). Decoded values map to the seeded commodity_spec_schemas
      facet keys/enum values; record_spec independently re-validates them.
Called by: the enrichment worker's second pass (see services that call decode_mpn) and
      scripts/decode_mpn_dryrun.py.
Depends on: nothing (pure functions).

Coverage is deliberately CONSERVATIVE: a decoder emits a spec only when the part-number
scheme expresses it unambiguously. Expand per-vendor tables as the dry-run surfaces real
inventory. See docs/superpowers/specs/2026-06-08-mpn-decode-enrichment-design.md.
"""

from app.services.mpn_decoder._common import DecodeResult
from app.services.mpn_decoder.memory import decode_memory
from app.services.mpn_decoder.storage import decode_storage
from app.utils.normalization import normalize_mpn

# Ordered: storage first, then memory. Each returns a DecodeResult or None.
_DECODERS = (decode_storage, decode_memory)


def decode_mpn(mpn: str | None, manufacturer: str | None = None) -> DecodeResult | None:
    """Decode a standard manufacturer MPN to a DecodeResult, or None if unrecognized.

    `manufacturer` is an optional hint; decoders gate on the MPN string itself, so a wrong
    or missing manufacturer never causes a misdecode (the regex gate is the source of truth).
    """
    normalized = normalize_mpn(mpn)  # upper + strip quotes/whitespace/trailing punct; keeps -, /
    if not normalized:
        return None
    for decoder in _DECODERS:
        result = decoder(normalized, manufacturer)
        if result is not None and result.specs:
            return result
    return None


__all__ = ["DecodeResult", "decode_mpn"]

"""MPN normalizer for search worker deduplication.

Normalizes manufacturer part numbers by stripping whitespace,
uppercasing, and removing well-known ordering/packaging suffixes
that don't change the actual part identity.

Called by: queue_manager.enqueue_search()
Depends on: nothing (pure function)
"""

import re

# Well-known ordering/packaging suffixes to strip.
# Only strip suffixes that are clearly noise — be conservative.
# Format: compiled regex patterns matching end of string.
_STRIP_SUFFIXES = re.compile(
    r"("
    r"/TR|"  # Tape and reel (slash form)
    r"-TR|"  # Tape and reel (dash form)
    r"/CT|"  # Cut tape
    r"-CT|"  # Cut tape (dash form)
    r"-ND|"  # No datasheet (DigiKey)
    r"-DKR|"  # DigiKey reel packaging
    r"#PBF|"  # Lead-free indicator
    r"-PBF|"  # Lead-free indicator (dash form)
    r"/NOPB|"  # No-lead (TI convention)
    r"-NOPB|"  # No-lead (dash form)
    r"-TRPBF|"  # Combined tape-and-reel + lead-free (e.g. ON Semi, ST)
    r"/TRPBF|"  # Combined tape-and-reel + lead-free (slash form)
    r"\+TR|"  # Maxim tape-and-reel indicator (base part ends "+", reel is "+TR")
    r"-E3|"  # Vishay/ON Semi Pb-free grade suffix (e.g. SS14-E3)
    r"-E4"  # Vishay/ON Semi Pb-free grade suffix, alt lot (e.g. SS14-E4)
    r")$",
    re.IGNORECASE,
)

# NOTE on -RL (reel): Some manufacturers use -RL as a package code
# (e.g. ADP3338AKCZ-3.3-RL where -RL means reel packaging).
# We strip it because it's a packaging suffix, not a part identifier.
# The base part ADP3338AKCZ-3.3 is the same component.
_REEL_SUFFIX = re.compile(r"-RL\d*$", re.IGNORECASE)

# NOTE on -TR<digits> / /TR<digits> (reel-size variants, e.g. -TR13, /TR7):
# Same shape as -RL<digits> above — "TR" immediately followed by a digit run
# is a reel-count/reel-size code, not a plausible MPN body ending. Handled as
# its own pattern (rather than folded into _STRIP_SUFFIXES) because the bare
# "-TR"/"/TR" alternatives above are exact (no trailing digits allowed), and
# digits directly after "TR" only ever mean "reel size" in the packaging
# conventions we've seen (e.g. Analog Devices/TI 7" vs 13" reel codes).
_TR_RESEL_SUFFIX = re.compile(r"[-/]TR\d+$", re.IGNORECASE)

# Suffixes we deliberately do NOT strip, and why (do not add these later
# without a concrete false-merge report backing the change):
#   -13, -7, etc. (bare digit suffix, no "TR"/"RL" prefix) — a bare trailing
#       digit run is frequently part of the base MPN itself (voltage/current/
#       package variant, e.g. many op-amp/regulator families use "-13" as a
#       distinct catalog variant, not a reel code). Only strip digit runs when
#       unambiguously prefixed by a known packaging token ("TR"/"RL").
#   -Q1 — AEC-Q100 automotive qualification grade. Changes the part's
#       qualification/reliability class; TI, ON Semi, etc. sell -Q1 and
#       non-Q1 variants as genuinely different SKUs with different pricing
#       and specs. Stripping would falsely merge automotive-grade and
#       commercial-grade parts.
#   -EP — "Enhanced Product" (TI) / similarly-named grades from other
#       manufacturers denote a controlled baseline/extended-reliability
#       variant, again a distinct sellable SKU, not packaging noise.
#   -T (bare, single letter) — too ambiguous: some manufacturers (e.g.
#       Maxim/older Linear parts) use a bare trailing "T" for tape-and-reel,
#       but "T" is also a common terminal letter in temperature-grade and
#       package-code suffixes baked into the base part number itself (e.g.
#       "LM317T" — the "T" is the TO-220 package designator, not a reel
#       code; stripping it would merge LM317T with LM317, a real,
#       differently-packaged part). No safe general rule without a
#       manufacturer-specific suffix table, so left unstripped.


def strip_packaging_suffixes(mpn: str) -> str:
    """Normalize an MPN for deduplication comparison.

    - Uppercase
    - Strip all whitespace
    - Strip common packaging/ordering suffixes
    - Keep meaningful suffixes (package codes, temperature grades)

    Returns the normalized string.
    """
    if not mpn:
        return ""
    result = mpn.strip().upper()
    result = re.sub(r"\s+", "", result)
    result = _STRIP_SUFFIXES.sub("", result)
    result = _REEL_SUFFIX.sub("", result)
    result = _TR_RESEL_SUFFIX.sub("", result)
    return result

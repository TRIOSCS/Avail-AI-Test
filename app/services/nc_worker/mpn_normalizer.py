"""MPN normalizer for NetComponents deduplication.

Normalizes manufacturer part numbers by stripping whitespace,
uppercasing, and removing well-known ordering/packaging suffixes
that don't change the actual part identity.

Called by: queue_manager.enqueue_for_nc_search()
Depends on: nothing (pure function)
"""

import re

# Well-known ordering/packaging suffixes to strip.
# Only strip suffixes that are clearly noise — be conservative.
# Format: compiled regex patterns matching end of string.
_STRIP_SUFFIXES = re.compile(
    r"("
    r"/TR|"      # Tape and reel (slash form)
    r"-TR|"      # Tape and reel (dash form)
    r"/CT|"      # Cut tape
    r"-CT|"      # Cut tape (dash form)
    r"-ND|"      # No datasheet (DigiKey)
    r"-DKR|"     # DigiKey reel packaging
    r"#PBF|"     # Lead-free indicator
    r"-PBF|"     # Lead-free indicator (dash form)
    r"/NOPB|"    # No-lead (TI convention)
    r"-NOPB"     # No-lead (dash form)
    r")$",
    re.IGNORECASE,
)

# NOTE on -RL (reel): Some manufacturers use -RL as a package code
# (e.g. ADP3338AKCZ-3.3-RL where -RL means reel packaging).
# We strip it because it's a packaging suffix, not a part identifier.
# The base part ADP3338AKCZ-3.3 is the same component.
_REEL_SUFFIX = re.compile(r"-RL\d*$", re.IGNORECASE)


def normalize_mpn(mpn: str) -> str:
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
    return result

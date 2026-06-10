"""Deterministic display/panel description→spec extraction (TRIO inventory grammar).

What: reads resolution / diagonal size / backlight out of compact human panel
      descriptions like ``PNL,15.6 FHD AG WLED SVA 45% 220neDP,INX`` or
      ``LCD, 21.5", LG`` — NO network, NO LLM. Every emitted value is a seeded
      displays enum member / in-range numeric per app/data/commodity_seeds.json;
      record_spec independently re-validates enum members and skips unseeded
      keys, but numeric ranges are enforced ONLY here — the drift guard in
      tests/test_desc_extractor_routing.py pins both against the seeds.
Called by: app/services/desc_extractor/__init__.py (extract_desc routing).
Depends on: _common (constants only) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- resolution from named classes (HD/FHD/QHD/WUXGA/UHD/4K) and explicit WxH pixel
  pairs that are seeded members. ``HD+`` is excluded by ``(?!\\+)`` and ``WXGA``
  is deliberately unmapped (1280x800-vs-1366x768 ambiguity); glued tokens
  ("FRUDummy14FHD", "FHDI") never match — the boundary kills them. Two DISTINCT
  pixel values ⇒ omit (the same value from both grammars is fine).
- diagonal_size only from an explicit inch mark (21.5" / 21.5-IN / 27inch) or a
  decimal size immediately before a named resolution class ("15.6 FHD"); bare
  integers ("HU, FHD … 13 TS") and width markers ("15.6W WXGA") are deliberate
  misses. Candidates filtered to the seeded 7-86 range; unique-or-omit.
- backlight: WLED (white LED) and bare LED on TRIO panels are the same white
  bucket — both map to the generic seeded "LED" member. Never emit the seeded
  "LED White"/"LED RGB" members from descriptions (white-vs-RGB is not
  expressible from TRIO descs: the RGB in "…,WLED,250,RGB,…" is the color
  interface). OLED never matches (no boundary inside); EL/CCFL unmapped.
"""

import re

# Canonical displays enum strings — MUST match the displays entry in
# app/data/commodity_seeds.json (drift-guarded).
LED = "LED"
_RES_BY_NAME = {
    "WUXGA": "1920x1200",
    "UHD": "3840x2160",
    "4K": "3840x2160",
    "QHD": "2560x1440",
    "FHD": "1920x1080",
    "HD": "1366x768",
}
# Seeded displays.resolution pixel members an explicit WxH token may emit
# (3-4 digit pairs only — the 16x2/20x4 character formats are unreachable).
_RES_SEEDED = {
    "240x320",
    "320x240",
    "480x272",
    "800x480",
    "1024x600",
    "1280x800",
    "1920x1080",
    "1366x768",
    "1920x1200",
    "2560x1440",
    "3840x2160",
}

_RES_NAMED = re.compile(r"\b(WUXGA|UHD|QHD|FHD|HD)(?!\+)\b|\b4K\b")
_RES_EXPLICIT = re.compile(r"\b(\d{3,4})\s?X\s?(\d{3,4})\b")
_DIAG_INCH = re.compile(r"\b(\d{1,2}(?:\.\d{1,2})?)\s?(?:\"|''|[- ]?IN(?:CH(?:ES)?)?\b)")
_DIAG_BEFORE_RES = re.compile(r"\b(\d{1,2}\.\d)\s+(?=(?:FHD|UHD|QHD|WUXGA|HD)(?!\+)\b)")
_BACKLIGHT = re.compile(r"\bW?LED\b")

# Seeded displays.diagonal_size numeric_range — the only range gate (record_spec
# performs no numeric_range check); pinned against the seeds by the drift guard.
_DIAG_MIN, _DIAG_MAX = 7, 86


def _resolution(text: str) -> str | None:
    """Distinct surviving seeded resolution member, or None (absent / conflict)."""
    values = {_RES_BY_NAME[m.group(1) or "4K"] for m in _RES_NAMED.finditer(text)}
    for m in _RES_EXPLICIT.finditer(text):
        explicit = f"{m.group(1)}x{m.group(2)}"
        if explicit in _RES_SEEDED:
            values.add(explicit)
    return values.pop() if len(values) == 1 else None


def _diagonal_size(text: str) -> int | float | None:
    """Distinct surviving diagonal-inch candidate in the seeded range, or None."""
    values = {float(m.group(1)) for m in _DIAG_INCH.finditer(text)}
    values |= {float(m.group(1)) for m in _DIAG_BEFORE_RES.finditer(text)}
    values = {v for v in values if _DIAG_MIN <= v <= _DIAG_MAX}
    if len(values) != 1:
        return None
    value = values.pop()
    return int(value) if value.is_integer() else value


def extract_display(text: str) -> dict[str, str | int | float]:
    """Extract displays specs from an upper-cased, whitespace-collapsed description."""
    specs: dict[str, str | int | float] = {}
    resolution = _resolution(text)
    if resolution is not None:
        specs["resolution"] = resolution
    diagonal = _diagonal_size(text)
    if diagonal is not None:
        specs["diagonal_size"] = diagonal
    if _BACKLIGHT.search(text):
        specs["backlight"] = LED
    return specs

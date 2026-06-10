"""Deterministic motherboard description→spec extraction (TRIO inventory grammar).

What: reads the board type out of compact human board descriptions like
      ``MB, B82CD NOK A49120C UMA 4G32G`` or ``BDPLANAR WIN,i5-10210U,16G,9560,
      yTPM2`` — NO network, NO LLM. Every emitted value is a seeded motherboards
      enum member per app/data/commodity_seeds.json; record_spec independently
      re-validates enum members and skips unseeded keys. The drift guard in
      tests/test_desc_extractor_routing.py pins the vocabulary against the seeds.
Called by: app/services/desc_extractor/__init__.py (extract_desc routing).
Depends on: _common (constants only) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- board_type is a closed token→member map; multiple DISTINCT members ⇒ omit
  ("MB, 7063-CR1 System backplane kit" is a kit, not one board — correct omit).
- The bare MB token carries a lookbehind that kills spaced megabytes ("512 MB");
  glued "512MB"/"36MB" never had a boundary. Mis-filed MB-bucket rows without a
  board token ("Function Board C 82L7 IO", "TOUCHPAD", "HS 2TB 3.5") emit {}.
- The route has no GB key at all, so dram-looking tokens in laptop-board specs
  ("ASSY, MB DSC 1050 4GB i7-8750H HM370 WIN") are inert by construction.
- NOT extracted: onboard_cpu_family (not seeded — phase-3 candidate with its own
  seed), socket/chipset/ATX form_factor (≤0.4% corpus fill), laptop-vs-desktop
  split (UMA/DSC/NBK are OEM-specific noise, not deterministic tokens).
"""

import re

# Canonical board_type enum strings — MUST match the motherboards entry in
# app/data/commodity_seeds.json (drift-guarded).
SYSTEM_BOARD, BACKPLANE, RISER, DAUGHTER_BOARD = "System Board", "Backplane", "Riser", "Daughter Board"

_BOARD_PATTERNS = (
    (
        SYSTEM_BOARD,
        re.compile(
            r"\bBDPLANAR\b|\bPLANAR\b|SYSTEM BOARD|\bMOTHERBOARD\b|\bMAINBOARD\b"
            r"|\bMAIN BOARD\b|\bBD SYS\b|(?<![\d.] )\bMB\b"
        ),
    ),
    (BACKPLANE, re.compile(r"\bBACKPLANE\b")),
    (RISER, re.compile(r"\bRISER\b")),
    (DAUGHTER_BOARD, re.compile(r"DAUGHTER\s?(?:CARD|BOARD)")),
)


def _board_type(text: str) -> str | None:
    """Seeded board_type member, or None (no token / conflicting members)."""
    members = {member for member, pattern in _BOARD_PATTERNS if pattern.search(text)}
    return members.pop() if len(members) == 1 else None


def extract_board(text: str) -> dict[str, str]:
    """Extract motherboards specs from an upper-cased, whitespace-collapsed
    description."""
    specs: dict[str, str] = {}
    board_type = _board_type(text)
    if board_type is not None:
        specs["board_type"] = board_type
    return specs

"""Grammar-gated category inference from a human description (the CATEGORIZE stage).

What: ``categorize_from_desc`` reads a strict commodity *lead/body grammar* out of a
      TRIO description and returns the canonical commodity KEY it unambiguously names
      (``hdd``/``ssd``/``dram``/``cpu``/``power_supplies``/``displays``/``tape_drives``/
      ``gpu``/``motherboards``/``cables``/``batteries``/``fans_cooling``) — or ``None``
      when nothing is safe to say. It exists so an UNCATEGORIZED card can be categorized
      from its own description before the spec extractors run (they require a category).
      Same discipline as the spec extractors (desc_extractor/__init__.py): a foreign
      lead suppresses, conflicting signals return None, "a wrong facet is worse than a
      missing one" — here, "a wrong CATEGORY is worse than a missing one".

      The eleven SPEC_COMMODITIES are routed by reusing ``extract_desc``'s own lead/body/
      contradiction machinery (no second grammar to drift): ``extract_desc(description)``
      with NO hint returns a DescResult whose ``commodity`` IS the routing verdict, and
      it already encodes every foreign-lead / packaging-lead / storage×dram-conflict /
      CPU-pollution rule. For ``cpu`` we additionally require an explicit CPU-identity
      token (Xeon / Core iN / Ryzen / EPYC / model string / "PROCESSOR" lead — the plan's
      ``CPU + Xeon/Core/Ryzen`` gate) so a bare ``CPU,`` lead on a non-CPU spare never
      mints the heavily-polluted cpu category.

      The three categories ``extract_desc`` does NOT handle (cables / batteries /
      fans_cooling) have their own conservative ANCHORED lead grammars with explicit
      pollution suppression (e.g. "BATTERY MANAGEMENT" ICs are NOT batteries). They are
      checked first and only fire on a start-of-string lead, so an "…ANTENNA CABLE"
      buried inside another commodity's row never routes here.

Called by: app/services/desc_extractor/writer.py (categorize_and_record — the CATEGORIZE
      stage), app/management/categorize_from_desc.py (the one-shot CLI),
      app/services/source_ingest/clean.py (ingest-time fallback when the source carries
      no mappable category — single source of truth for the grammar).
Depends on: desc_extractor.extract_desc (the spec-commodity router) only. Pure — no DB,
      no network, no LLM.
"""

import re

from app.services.desc_extractor import extract_desc

# Whitespace / Excel-CR normalization mirrors extract_desc so the lead anchors line up
# ("CABLE,\n…", "BATTERY_x000D_…"). Upper-cased once; the new-category grammars below are
# all anchored, case-folded shapes.
_WS = re.compile(r"\s+")
_X000D = re.compile(r"_x000[dD]_")

# CPU-IDENTITY gate (the plan's ``CPU/PROCESSOR + Xeon/Core/Ryzen`` rule): a bare
# ``CPU,``/``PROCESSOR,`` lead is NOT enough to mint the ~14%-polluted cpu category — the
# description must carry an explicit CPU family word or model string (Intel Core iN /
# Xeon / AMD Ryzen / EPYC / Threadripper / Atom / Itanium / Opteron / Pentium / Celeron /
# Athlon, an E3/E5/E7 or Core-iN model number, or a Scalable Gold/Silver/Platinum/Bronze
# 3-9xxx). The bare word "PROCESSOR" alone qualifies (TRIO's own commodity label), but
# "CPU"/"PROC" alone do NOT — those are the polluted leads. extract_desc has already
# applied its own is_cpu_pollution deny-list, so this is the second, stricter gate.
# NB: the model-number alternatives carry NO trailing ``\b`` — Xeon V-suffix shapes
# ("E5-2650L", "E5-2650 V4") and trailing-letter Core SKUs ("I7-7700HQ") would fail a
# closing word boundary. This mirrors ``__init__._CPU_WEAK`` exactly.
_CPU_IDENTITY = re.compile(
    r"\bPROCESSOR\b|\bXEON\b|\bEPYC\b|\bRYZEN\b|\bTHREADRIPPER\b|\bATOM\b"
    r"|\bITANIUM ?2?\b|\bOPTERON\b|\bPENTIUM\b|\bCELERON\b|\bATHLON\b"
    r"|\bCORE ?I[3579]\b|\bI[3579]-\d{4,5}|\bE[357]-?\d{4}"
    r"|\b(?:GOLD|SILVER|PLATINUM|BRONZE)[ -]?[3-9]\d{3}[A-VX-Z]?\b"
)

# ── cables ───────────────────────────────────────────────────────────────────
# Lead "CABLE,"/"CABLE "/"CBL," is the TRIO commodity label and is unambiguous: the
# corpus's CABLE-led rows are genuine cables (LVDS / USB / QSFP / power cords). Anchored
# at the start ONLY — a trailing "…ANTENNA CABLE" inside another commodity's row leads
# with its own commodity, so it never routes here.
_CABLE_LEAD = re.compile(r"^(?:CABLE|CBL)\b")

# ── fans / cooling ─────────────────────────────────────────────────────────────
# Lead FAN/HEATSINK/HEAT SINK/HSF/BLOWER → fans_cooling. The corpus's FAN-/HEATSINK-led
# rows are blowers, heatsink-fan assemblies, thermal modules — all fans_cooling. A bare
# "HEAT SINK," with no fan is still thermal-cooling hardware in this bucket.
_FAN_LEAD = re.compile(r"^(?:FAN|HEATSINK|HEAT SINK|HSF|BLOWER)\b")

# ── batteries ──────────────────────────────────────────────────────────────────
# Lead BATTERY/BATT/BTRY → batteries, EXCEPT the "BATTERY MANAGEMENT"/gas-gauge IC class
# (BQ40Z50 / LTC6803 fuel-gauge / stack-monitor ICs describe themselves as "BATTERY
# MANAGEMENT …" — a chip, not a battery). Suppress that shape; everything else
# BATTERY-led (cells, UPS lead-acid, NVRAM cache batteries, laptop packs) categorizes.
_BATTERY_LEAD = re.compile(r"^(?:BATTERY|BATT|BTRY)\b")
_BATTERY_POLLUTION = re.compile(r"\bBATTERY MANAGEMENT\b|\bGAS GAUGE\b|\bFUEL GAUGE\b|\bSTACK MONITOR\b")

# New categories not handled by extract_desc, in suppression-aware match order. cables is
# checked before fans/batteries so the rare "CABLE … FAN" lead row stays a cable.
_NEW_CATEGORY_LEADS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cables", _CABLE_LEAD),
    ("fans_cooling", _FAN_LEAD),
    ("batteries", _BATTERY_LEAD),
)


def _norm(description: str) -> str:
    """Upper-cased, Excel-CR-scrubbed, whitespace-collapsed text — the shape the
    anchored new-category lead grammars match against (mirrors extract_desc's own
    normalization so a lead lines up identically in both stages)."""
    text = _X000D.sub(" ", description)
    return _WS.sub(" ", text).strip().upper()


def categorize_from_desc(description: str | None) -> str | None:
    """Return the canonical commodity key a description unambiguously names, or None.

    Routing order:
      1. The three categories ``extract_desc`` does not cover (cables / batteries /
         fans_cooling) via their own ANCHORED lead grammars + pollution suppression.
      2. Everything else delegates to ``extract_desc(description)`` (NO hint) and uses the
         returned ``commodity`` — which already encodes the foreign-lead, packaging-lead,
         storage×dram-conflict and CPU-pollution rules. For ``cpu`` the result is gated a
         SECOND time on an explicit CPU-identity token so a bare ``CPU,`` spare row is not
         categorized.

    Conservative by construction: anything that does not anchor a known lead and does not
    yield an extract_desc commodity returns None (the card stays uncategorized — a wrong
    category is worse than a missing one). The returned key is always a canonical
    commodity_seeds key, so set_category's normalize_category never drops it as off-vocab.
    """
    if not description:
        return None
    text = _norm(description)
    if not text:
        return None

    # 1. New-category anchored leads (extract_desc has no grammar for these).
    for commodity, lead in _NEW_CATEGORY_LEADS:
        if lead.match(text):
            if commodity == "batteries" and _BATTERY_POLLUTION.search(text):
                return None  # "BATTERY MANAGEMENT" IC, not a battery — never categorize
            return commodity

    # 2. The eleven SPEC_COMMODITIES via extract_desc's own router (no hint → pure routing).
    result = extract_desc(description)
    if result is None:
        return None
    if result.commodity == "cpu" and not _CPU_IDENTITY.search(text):
        # extract_desc routed cpu (a CPU/PROC body token or _CPU_WEAK family word), but
        # the stricter categorize gate needs explicit CPU identity — a bare "CPU," spare
        # with no Xeon/Core/Ryzen/model string is not safe to mint as cpu.
        return None
    return result.commodity


__all__ = ["categorize_from_desc"]

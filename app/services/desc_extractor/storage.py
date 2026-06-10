"""Deterministic hdd/ssd description→spec extraction (TRIO inventory grammar).

What: reads capacity / rpm / form factor / interface out of compact human drive
      descriptions like ``HD, 450GB, 15KRPM, 3.5", Fibre Channel`` or
      ``4TB 7.2K Rpm 3.5inch 12gbps Sas HDD`` — NO network, NO LLM. Every emitted
      value is a seeded commodity_spec_schemas enum member / valid numeric for the
      routed commodity; record_spec independently re-validates enum members and
      skips unseeded keys (it performs no numeric_range check — capacity sanity
      lives in the link-speed exclusions here).
Called by: app/services/desc_extractor/__init__.py (extract_desc routing).
Depends on: _common (constants only) — pure functions.

CONSERVATIVE by design (a wrong facet value is worse than a missing one):
- Capacity requires an explicit unit token (GB/G/TB). Link-speed tokens are excluded
  three ways: ``6Gbps``/``12gbps`` never match (no word boundary before "bps"),
  ``6Gb/s`` is rejected by the trailing "/S" check, and bare ``6Gb``/``4Gb``/``16GB``
  link-generation values (SAS/SATA/FC: 1/1.5/2/3/4/6/8/12/16/22.5/24/32) are
  discarded outright — so tiny legacy capacities in that set are deliberately missed
  rather than ever mistaking a link speed for a capacity.
- Conflicting signals for a key (two different capacities, 2.5" + 3.5", SAS + SATA)
  ⇒ that key is omitted, the rest still extract.
- Speed-qualified interfaces ("6Gbps SAS") collapse to the bare seeded enum member
  (the seeds carry no speed-qualified SATA/SAS/SCSI entries; the ssd seeds DO carry
  generation-qualified "NVMe PCIe 3.0/4.0/5.0" members, but NVMe is not in the ssd
  _IFACE_VOCAB here, so this extractor never emits it on the ssd route).
- Per-commodity vocabulary gating: rpm is hdd-only; ssd form_factor only accepts
  2.5" (3.5"/1.8" are not seeded ssd members); bare "NVMe" and "FC" are seeded for
  hdd but not ssd, so they are omitted on the ssd route.
"""

import re

# Canonical enum strings — MUST match the hdd/ssd entries in app/data/commodity_seeds.json.
_FF_BY_VALUE = {"2.5": '2.5"', "3.5": '3.5"', "1.8": '1.8"'}
_FF_VOCAB = {"hdd": {'2.5"', '3.5"', '1.8"'}, "ssd": {'2.5"'}}
_IFACE_VOCAB = {"hdd": {"SATA", "SAS", "SCSI", "NVMe", "FC"}, "ssd": {"SATA", "SAS", "SCSI"}}
_RPM_VOCAB = {5400: "5400", 7200: "7200", 10000: "10000", 15000: "15000"}

# Bare-Gb values that are link-speed generations, never drive capacities, in TRIO's
# inventory grammar: SATA 1.5/3/6, SAS 3/6/12/22.5/24, FC 1/2/4/8/16/32.
_LINK_SPEED_GB = {1, 1.5, 2, 3, 4, 6, 8, 12, 16, 22.5, 24, 32}

_CAPACITY = re.compile(r"\b(\d{1,5}(?:\.\d{1,2})?)\s?(TB|GB|G)\b")
# rpm grammars seen in the corpus: "15K"/"15KRPM"/"7.2K"/"15K RPM" | "15,000 RPM"/
# "7200RPM" | "5400R". A digit hugging the K ("10K8", "C10K900", "SS8K") kills the
# word boundary, so family codes and sector tokens never match.
_RPM_K = re.compile(r"\b(\d{1,2}(?:[.,]\d)?)\s?K(?:\s?RPM)?\b")
_RPM_FULL = re.compile(r"\b(\d{1,2},?\d{3})\s?RPM\b")
_RPM_R = re.compile(r"\b(\d{4,5})R\b")
_FORM_INCH = re.compile(r"\b(\d\.\d)\s?(?:\"|''|[- ]?IN(?:CH(?:ES)?)?\b)")
_IFACE_PATTERNS = (
    ("SAS", re.compile(r"\bSAS\b")),
    ("SATA", re.compile(r"\bSATA(?:[- ]?(?:6G|3G|III|II))?\b")),
    ("SCSI", re.compile(r"\bSCSI\b")),
    ("NVMe", re.compile(r"\bNVME\b")),
    ("FC", re.compile(r"\bFC\b|\bFIBRE CHANNEL\b|\bFIBER CHANNEL\b")),
)


def _capacity_gb(text: str) -> int | float | None:
    """Distinct surviving capacity candidate, or None (no token / conflict)."""
    values: set[float] = set()
    for m in _CAPACITY.finditer(text):
        if text[m.end() : m.end() + 2] == "/S":  # "6Gb/s" — a link speed, not a size
            continue
        value = float(m.group(1))
        if m.group(2) == "TB":
            value *= 1000
        elif value in _LINK_SPEED_GB:  # bare "6Gb"/"4Gb"/"12Gb" link generations
            continue
        values.add(value)
    if len(values) != 1:
        return None  # nothing usable, or conflicting capacities — omit
    value = values.pop()
    return int(round(value)) if abs(value - round(value)) < 1e-6 else value


def _rpm(text: str) -> str | None:
    """Seeded rpm enum member ("5400"/"7200"/"10000"/"15000"), or None."""
    candidates: set[int] = set()
    for m in _RPM_K.finditer(text):
        candidates.add(int(round(float(m.group(1).replace(",", ".")) * 1000)))
    for m in _RPM_FULL.finditer(text):
        candidates.add(int(m.group(1).replace(",", "")))
    for m in _RPM_R.finditer(text):
        candidates.add(int(m.group(1)))
    # Non-seeded values (a "20K" token, a stray numeric) are dropped, never emitted;
    # two DIFFERENT seeded values would be a conflict — omit.
    seeded = {_RPM_VOCAB[c] for c in candidates if c in _RPM_VOCAB}
    return seeded.pop() if len(seeded) == 1 else None


def _form_factor(text: str, commodity: str) -> str | None:
    sizes = {m.group(1) for m in _FORM_INCH.finditer(text) if m.group(1) in _FF_BY_VALUE}
    if re.search(r"\bSFF\b", text):
        sizes.add("2.5")
    if re.search(r"\bLFF\b", text):
        sizes.add("3.5")
    if len(sizes) != 1:
        return None  # absent or conflicting (e.g. drive "2.5"" sold with a 3.5" kit)
    member = _FF_BY_VALUE[sizes.pop()]
    return member if member in _FF_VOCAB[commodity] else None


def _interface(text: str, commodity: str) -> str | None:
    hits = {name for name, pattern in _IFACE_PATTERNS if pattern.search(text)}
    if len(hits) != 1:
        return None  # absent or conflicting ("SAS/SATA" adapter-style descriptions)
    member = hits.pop()
    return member if member in _IFACE_VOCAB[commodity] else None


def extract_storage(text: str, commodity: str) -> dict[str, str | int | float]:
    """Extract hdd/ssd specs from an upper-cased, whitespace-collapsed description."""
    specs: dict[str, str | int | float] = {}
    capacity = _capacity_gb(text)
    if capacity is not None:
        specs["capacity_gb"] = capacity
    if commodity == "hdd":  # rpm is not a seeded ssd spec
        rpm = _rpm(text)
        if rpm is not None:
            specs["rpm"] = rpm
    form_factor = _form_factor(text, commodity)
    if form_factor is not None:
        specs["form_factor"] = form_factor
    interface = _interface(text, commodity)
    if interface is not None:
        specs["interface"] = interface
    return specs

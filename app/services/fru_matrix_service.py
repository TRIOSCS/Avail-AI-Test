"""FRU crosswalk read service — forward (FRU → everything) and reverse (PN → FRUs)
views.

Backs the materials detail "FRU matrix" / "Used in FRUs" panels and the
/v2/partials/materials/fru-lookup endpoint. Both entry points accept raw user/MPN
input and normalize it internally with normalize_mpn_key. The reverse view is
capped at REVERSE_VIEW_LIMIT usages (shared hardware PNs like screws can sit under
thousands of FRUs); ReverseView.total carries the uncapped count for display.

Called by: app/routers/htmx_views.py (material detail + fru-lookup partials, and the
           search-page "What we know" panel's compact FRU-crosswalk context)
Depends on: app/models/fru_link.FruLink, app/constants.FruLinkKind/CDC_PENDING,
            app/utils/normalization.normalize_mpn_key
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import CDC_PENDING, FruLinkKind
from ..models.fru_link import FruLink
from ..utils.normalization import normalize_mpn_key

# Max usages rendered in the "Used in FRUs" table (response/render size guard).
REVERSE_VIEW_LIMIT = 200

# Display labels per relationship kind (template-facing).
KIND_LABELS: dict[str, str] = {
    FruLinkKind.IBM_11S: "11S part number",
    FruLinkKind.MFG_MODEL: "Manufacturer model",
    FruLinkKind.OPTION: "Option",
    FruLinkKind.OPTION_PN: "Option PN",
    FruLinkKind.SOURCING_PN: "Sourcing PN",
    FruLinkKind.LENOVO_PN: "Lenovo PN",
    FruLinkKind.LENOVO_PPN: "Lenovo PPN",
    FruLinkKind.TRAY: "Tray",
    FruLinkKind.TRAY_ALT: "Alternate tray",
    FruLinkKind.BRACKET: "Bracket",
    FruLinkKind.BOARD: "Board",
    FruLinkKind.SCREWS: "Screws",
    FruLinkKind.SHUTTLE: "Shuttle",
    FruLinkKind.DONGLE: "Dongle",
    FruLinkKind.DRIVE_PN: "Drive PN",
    FruLinkKind.ASSEMBLY: "Assembly",
}

# Detail-panel sections: (section label, kinds in display order).
_SECTIONS: list[tuple[str, list[FruLinkKind]]] = [
    ("Approved drives & models", [FruLinkKind.DRIVE_PN, FruLinkKind.MFG_MODEL]),
    ("11S part numbers", [FruLinkKind.IBM_11S]),
    ("Options", [FruLinkKind.OPTION, FruLinkKind.OPTION_PN]),
    (
        "Trays & hardware",
        [
            FruLinkKind.TRAY,
            FruLinkKind.TRAY_ALT,
            FruLinkKind.BRACKET,
            FruLinkKind.BOARD,
            FruLinkKind.SCREWS,
            FruLinkKind.SHUTTLE,
            FruLinkKind.DONGLE,
        ],
    ),
    ("Lenovo PNs", [FruLinkKind.LENOVO_PN, FruLinkKind.LENOVO_PPN]),
    ("Sourcing & assembly", [FruLinkKind.SOURCING_PN, FruLinkKind.ASSEMBLY]),
]

# A kind missing from either mapping would silently vanish from the forward view
# (links are grouped strictly by _SECTIONS); fail at import instead.
assert {k for _, kinds in _SECTIONS for k in kinds} == set(FruLinkKind), "_SECTIONS must cover every FruLinkKind"
assert set(KIND_LABELS) == set(FruLinkKind), "KIND_LABELS must cover every FruLinkKind"


def _plural(n: int, noun: str) -> str:
    """'1 tray' / '3 trays' — display helper for compact summary lines."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


@dataclass(frozen=True)
class FruLinkItem:
    """One deduplicated related part under a FRU."""

    related_raw: str
    related_norm: str
    rel_kind: str
    kind_label: str
    manufacturer: str | None
    description: str | None
    qual_status: str | None
    qual_date: date | None
    note: str | None
    source_sheets: tuple[str, ...]

    @property
    def qual_is_pending(self) -> bool:
        return self.qual_status == CDC_PENDING

    @property
    def qual_label(self) -> str | None:
        if self.qual_status is None:
            return None
        return "CDC pending" if self.qual_is_pending else self.qual_status


@dataclass(frozen=True)
class FruSection:
    """A display section of the forward view (e.g. 'Trays & hardware')."""

    label: str
    items: tuple[FruLinkItem, ...]


@dataclass(frozen=True)
class FruView:
    """Everything the crosswalk knows about one FRU, grouped for display."""

    fru_raw: str
    fru_norm: str
    sections: tuple[FruSection, ...]
    series: tuple[str, ...]  # distinct series context across links
    machines: tuple[str, ...]  # distinct machine/platform context across links

    @property
    def total_links(self) -> int:
        """Count of sectioned (kind-mapped, deduplicated) items on display."""
        return sum(len(s.items) for s in self.sections)

    def _count_kinds(self, kinds: set[FruLinkKind]) -> int:
        return sum(1 for s in self.sections for i in s.items if i.rel_kind in kinds)

    @property
    def drive_count(self) -> int:
        """Items in the 'Approved drives & models' section (drive PNs + mfg models)."""
        return self._count_kinds({FruLinkKind.DRIVE_PN, FruLinkKind.MFG_MODEL})

    @property
    def ibm_11s_count(self) -> int:
        return self._count_kinds({FruLinkKind.IBM_11S})

    @property
    def tray_count(self) -> int:
        return self._count_kinds({FruLinkKind.TRAY, FruLinkKind.TRAY_ALT})

    @property
    def top_models(self) -> tuple[FruLinkItem, ...]:
        """First 3 manufacturer-model items (qualified-first order) for compact
        context."""
        models = [i for s in self.sections for i in s.items if i.rel_kind == FruLinkKind.MFG_MODEL]
        return tuple(models[:3])

    @property
    def summary(self) -> str:
        """One-line count summary for the search-page 'What we know' FRU context.

        Non-zero headline groups joined with '·'; falls back to the total link count
        when the FRU carries none of the headline kinds (e.g. Lenovo PNs only).
        """
        segments = [
            _plural(n, noun)
            for n, noun in (
                (self.drive_count, "approved drive"),
                (self.ibm_11s_count, "11S number"),
                (self.tray_count, "tray"),
            )
            if n
        ]
        if not segments:
            segments = [_plural(self.total_links, "linked part")]
        return " · ".join(segments)


@dataclass(frozen=True)
class FruUsage:
    """One FRU a part number appears under (reverse lookup)."""

    fru_raw: str
    fru_norm: str
    rel_kind: str
    kind_label: str
    manufacturer: str | None
    description: str | None
    qual_status: str | None
    series: str | None
    machine: str | None
    source_sheet: str

    @property
    def qual_is_pending(self) -> bool:
        return self.qual_status == CDC_PENDING

    @property
    def qual_label(self) -> str | None:
        if self.qual_status is None:
            return None
        return "CDC pending" if self.qual_is_pending else self.qual_status


@dataclass(frozen=True)
class ReverseView:
    """Reverse-lookup result: capped usages + the uncapped total for display."""

    usages: tuple[FruUsage, ...]
    total: int  # distinct (FRU, role) usages before the display cap

    @property
    def top_frus(self) -> tuple[str, ...]:
        """First 3 distinct FRU numbers (display spelling) for compact context."""
        return tuple(dict.fromkeys(u.fru_raw for u in self.usages))[:3]


def _richness(link: FruLink) -> tuple[int, int, int, int]:
    """Sort key: rows with qual_status, then manufacturer, then description first.

    link.id is the final tiebreaker so the chosen representative row is
    deterministic regardless of database row order.
    """
    return (
        0 if link.qual_status else 1,
        0 if link.manufacturer else 1,
        0 if link.description else 1,
        link.id,
    )


def _coalesce_items(links: list[FruLink]) -> list[FruLinkItem]:
    """Dedup links (same related_norm) across sheets, preferring richer rows.

    The winning row supplies the display values; missing attributes are filled from the
    duplicates so qual data from one sheet and manufacturer from another merge.
    """
    by_norm: dict[str, list[FruLink]] = {}
    for link in links:
        by_norm.setdefault(link.related_norm, []).append(link)

    items: list[FruLinkItem] = []
    for norm, dupes in by_norm.items():
        dupes.sort(key=_richness)
        best = dupes[0]
        manufacturer = next((d.manufacturer for d in dupes if d.manufacturer), None)
        description = next((d.description for d in dupes if d.description), None)
        qual_status = next((d.qual_status for d in dupes if d.qual_status), None)
        qual_date = next((d.qual_date for d in dupes if d.qual_date), None)
        note = next((d.note for d in dupes if d.note), None)
        sheets = tuple(dict.fromkeys(d.source_sheet for d in dupes))
        items.append(
            FruLinkItem(
                related_raw=best.related_raw,
                related_norm=norm,
                rel_kind=best.rel_kind,
                kind_label=KIND_LABELS.get(best.rel_kind, best.rel_kind),
                manufacturer=manufacturer,
                description=description,
                qual_status=qual_status,
                qual_date=qual_date,
                note=note,
                source_sheets=sheets,
            )
        )
    # Qualified parts first, then alphabetical for stable display.
    items.sort(key=lambda i: (0 if i.qual_status else 1, i.related_raw))
    return items


def get_fru_view(db: Session, mpn: str) -> FruView | None:
    """All crosswalk links for a FRU, grouped by kind into display sections.

    Returns None when the (normalized) input is unknown as a FRU.
    """
    norm = normalize_mpn_key(mpn)
    if not norm:
        return None
    # Defensive fetch cap: real FRUs carry well under 100 links; the cap bounds
    # memory/dedup work if a pathological key ever accumulates thousands of rows.
    links = db.execute(select(FruLink).where(FruLink.fru_norm == norm).order_by(FruLink.id).limit(5000)).scalars().all()
    if not links:
        return None

    by_kind: dict[str, list[FruLink]] = {}
    for link in links:
        by_kind.setdefault(link.rel_kind, []).append(link)

    sections: list[FruSection] = []
    for label, kinds in _SECTIONS:
        section_items: list[FruLinkItem] = []
        for kind in kinds:
            section_items.extend(_coalesce_items(by_kind.get(kind.value, [])))
        if section_items:
            sections.append(FruSection(label=label, items=tuple(section_items)))

    series = tuple(dict.fromkeys(link.series for link in links if link.series))
    machines = tuple(dict.fromkeys(link.machine for link in links if link.machine))
    # Sheets disagree on the raw spelling of the same FRU (Lenovo FRU-PN stores the
    # SAP-padded "0000000NV340_E00", Main stores "00NV340") — display the shortest
    # (canonical de-padded) form deterministically.
    fru_raw = min((link.fru_raw for link in links), key=lambda r: (len(r), r))
    return FruView(
        fru_raw=fru_raw,
        fru_norm=norm,
        sections=tuple(sections),
        series=series,
        machines=machines,
    )


def get_reverse_view(db: Session, mpn: str, limit: int = REVERSE_VIEW_LIMIT) -> ReverseView:
    """FRUs a part number appears under, with the role it plays in each.

    Deduplicates on (fru_norm, rel_kind) across sheets, preferring rows that carry
    qual_status/manufacturer context. Usages are capped at ``limit`` after a
    deterministic sort; ``total`` is the uncapped count. Returns an empty view when
    nothing matches.
    """
    norm = normalize_mpn_key(mpn)
    if not norm:
        return ReverseView(usages=(), total=0)
    # Defensive fetch cap (common hardware like trays/screws appears under MANY
    # FRUs): bounds the Python grouping below; display itself caps at ``limit``,
    # so the cap only affects the reported total on pathological parts.
    links = (
        db.execute(select(FruLink).where(FruLink.related_norm == norm).order_by(FruLink.id).limit(2000)).scalars().all()
    )
    if not links:
        return ReverseView(usages=(), total=0)

    by_key: dict[tuple[str, str], list[FruLink]] = {}
    for link in links:
        by_key.setdefault((link.fru_norm, link.rel_kind), []).append(link)

    usages: list[FruUsage] = []
    for (fru_norm, rel_kind), dupes in by_key.items():
        dupes.sort(key=_richness)
        best = dupes[0]
        usages.append(
            FruUsage(
                fru_raw=best.fru_raw,
                fru_norm=fru_norm,
                rel_kind=rel_kind,
                kind_label=KIND_LABELS.get(rel_kind, rel_kind),
                manufacturer=next((d.manufacturer for d in dupes if d.manufacturer), None),
                description=next((d.description for d in dupes if d.description), None),
                qual_status=next((d.qual_status for d in dupes if d.qual_status), None),
                series=next((d.series for d in dupes if d.series), None),
                machine=next((d.machine for d in dupes if d.machine), None),
                source_sheet=best.source_sheet,
            )
        )
    usages.sort(key=lambda u: (u.fru_raw, u.rel_kind))
    return ReverseView(usages=tuple(usages[:limit]), total=len(usages))

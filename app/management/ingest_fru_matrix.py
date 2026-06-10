"""Ingest the IBM/Lenovo "FRU_PN_TRAY matrix" workbook into the fru_links table.

Usage: python -m app.management.ingest_fru_matrix <xlsx> [--apply]

DEFAULT is a dry run: parses every mapped sheet and reports per-sheet parsed/skipped
row counts, per-kind link counts, and 10 sample links — writes nothing. With --apply
it upserts in chunks (insert new edges, refresh context attributes on existing ones,
keyed on fru_norm + related_norm + rel_kind + source_sheet).

Sheet → relationship mapping (kinds are FruLinkKind values):
  Main:               Part Number→ibm_11s, Option→option, Opt-PN→option_pn,
                      Model→mfg_model, Assembly→assembly,
                      Additional Sourcing Numbers→sourcing_pn (split),
                      Carrier→tray, Alternate Carrier→tray_alt
  Qlot/Gabor/CZ:      Drivepn→drive_pn (+qual status/date), Tray→tray,
                      Drive Model→mfg_model
  CDC NOT yet AP:     Drivepn→drive_pn with qual_status="cdc_pending"
  Lenovo-HDD:         Part Number→ibm_11s, Lenovo/Idea PN→lenovo_pn,
                      Model→mfg_model, Carrier→tray (FW → note)
  Lenovo FRU-PN:      PPN→lenovo_ppn (FRU de-padded: _Exx suffix + zero padding)
  LVN VPD Mapping:    Option P/N→option, MFG Model→mfg_model,
                      Make-to-Label→sourcing_pn, Tray P/N→tray
  Series:             partno→ibm_11s, idmodelo→mfg_model, bracket(+alt)→bracket,
                      board(+alt)→board, screws→screws
  NSeries(NetApp):    per-vendor MFG P/N→mfg_model, Shuttle→shuttle, Dongle→dongle,
                      Dongle Screw→screws (FRU forward-filled down blank rows)
Skipped sheets: "11s Sub Check", "HD Matrix Template", "Lenovo HD-SD Template"
(lookup tools/templates) and "Key Table" (test/disposition codes, no part links).

Called by: admin manually after receiving an updated FRU matrix workbook
Depends on: openpyxl, app.models.FruLink, app.constants.FruLinkKind,
            app.utils.normalization.normalize_mpn_key
"""

import argparse
import re
from collections import Counter
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from typing import Iterable

import sqlalchemy as sa
from loguru import logger

from app.constants import FruLinkKind
from app.utils.normalization import normalize_mpn_key

# ── Cell hygiene ──────────────────────────────────────────────────────

# Values that mean "no data" anywhere in the workbook (case-insensitive).
# "yes"/"no"/"na"/"x" appear as filler in PN columns (e.g. Lenovo-HDD Carrier="Yes").
_SENTINELS = {"", "n/a", "n/ a", "#n/a", "pendiente", "none", "na", "yes", "no", "x", "-", "?"}

# A plausible part number: starts alphanumeric, no whitespace, 3-40 chars.
# Prose cells ("Approved sub is 81Y9727 w/ same 11S PN ...") fail this and are dropped.
_PN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./+_-]{2,39}$")

_PAREN_RE = re.compile(r"\(([^)]*)\)")
_LENOVO_FRU_SUFFIX_RE = re.compile(r"_[A-Za-z]\d{1,3}$")  # e.g. "_E00"

# Lenovo platform/series labels that show up in Brand columns — NOT manufacturers.
_LENOVO_PLATFORM_BRANDS = {"lenovo", "lenovo storage", "system x", "thinksystem", "thinkserver", "ideapad"}


def _clean(value) -> str | None:
    """Trim, de-nbsp, collapse whitespace; map sentinel values to None."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    if s.lower() in _SENTINELS:
        return None
    return s


def _pn(value) -> str | None:
    """A single plausible part number, or None (sentinels, prose, blanks)."""
    s = _clean(value)
    if not s or not _PN_RE.match(s):
        return None
    return s


def _split_pns(value, seps: str = r"[,;\n]") -> list[str]:
    """Split a multi-value cell and keep only plausible part numbers."""
    s = _clean(value)
    if not s:
        return []
    return [p for p in (_pn(tok) for tok in re.split(seps, s)) if p]


def _parse_carrier(value) -> tuple[list[str], str | None]:
    """Carrier cells: split on '/' (and , ; newline), parentheticals become the note.

    "SM10G01157 / 00FC544 (Blue hot-swap)" → (["SM10G01157", "00FC544"], "Blue hot-swap")
    """
    s = _clean(value)
    if not s:
        return [], None
    notes = [n.strip() for n in _PAREN_RE.findall(s) if n.strip()]
    bare = _PAREN_RE.sub(" ", s)
    pns = [p for p in (_pn(tok) for tok in re.split(r"[/,;\n]", bare)) if p]
    return pns, ("; ".join(notes) or None)


def _lenovo_fru(value) -> tuple[str, str] | None:
    """Lenovo FRU-PN FRU cell → (raw, norm). "0000000NV340_E00" → norm of "00NV340".

    Strips the ``_Exx`` revision suffix, then the zero-padding group down to the
    7-char FRU (only when everything before the last 7 chars is zeros — 10-char PNs
    like "SB17B49754" are left alone).
    """
    s = _clean(value)
    if not s:
        return None
    base = _LENOVO_FRU_SUFFIX_RE.sub("", s)
    if len(base) > 7 and not base[: len(base) - 7].strip("0"):
        base = base[-7:]
    norm = normalize_mpn_key(base)
    if len(norm) < 3:
        return None
    return s, norm


def _date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _num(value) -> str | None:
    """Compact number for note strings: 9.0 → "9", 3.5 → "3.5"."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    s = _clean(value)
    return s


def _join_notes(*parts: str | None) -> str | None:
    return "; ".join(p for p in parts if p) or None


def _pad(row: tuple, width: int) -> tuple:
    """read_only rows can be shorter than the header — pad with None."""
    return row + (None,) * (width - len(row)) if len(row) < width else row


def _is_empty(row: tuple) -> bool:
    """True when every cell is None/whitespace (trailing formatting rows)."""
    return not any(c is not None and str(c).strip() for c in row)


# ── Parsed link record ────────────────────────────────────────────────


@dataclass
class ParsedLink:
    """One FRU↔PN edge parsed from a sheet (mirrors FruLink columns)."""

    fru_raw: str
    fru_norm: str
    related_raw: str
    related_norm: str
    rel_kind: str
    source_sheet: str
    manufacturer: str | None = None
    description: str | None = None
    series: str | None = None
    machine: str | None = None
    qual_status: str | None = None
    qual_date: date | None = None
    note: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.fru_norm, self.related_norm, self.rel_kind, self.source_sheet)


@dataclass
class SheetStats:
    """Dry-run/apply report numbers for one sheet."""

    sheet: str
    rows: int = 0  # data rows scanned
    empty_rows: int = 0  # fully blank rows (sheet formatting padding)
    skipped_rows: int = 0  # rows with data but no usable FRU
    duplicate_links: int = 0  # parsed edges collapsed into an existing key
    kinds: Counter = field(default_factory=Counter)  # unique links per rel_kind


_IDENTITY_FIELDS = {"fru_raw", "fru_norm", "related_raw", "related_norm", "rel_kind", "source_sheet"}


class _Emitter:
    """Per-sheet link collector: validates, dedups on the unique key, coalesces."""

    _CONTEXT_FIELDS = [f.name for f in fields(ParsedLink) if f.name not in _IDENTITY_FIELDS]

    def __init__(self, sheet: str):
        self.sheet = sheet
        self.stats = SheetStats(sheet=sheet)
        self._by_key: dict[tuple, ParsedLink] = {}

    def scan(self, row: tuple) -> bool:
        """Count the row; returns False (and counts it as empty) for blank rows."""
        self.stats.rows += 1
        if _is_empty(row):
            self.stats.empty_rows += 1
            return False
        return True

    def add(
        self,
        fru_raw: str,
        fru_norm: str,
        related: str | None,
        kind: FruLinkKind,
        related_norm_override: str | None = None,
        **context,
    ) -> None:
        if not related:
            return
        related_norm = related_norm_override or normalize_mpn_key(related)
        if not related_norm or related_norm == fru_norm:
            return  # unusable or self-edge
        link = ParsedLink(
            fru_raw=fru_raw,
            fru_norm=fru_norm,
            related_raw=related,
            related_norm=related_norm,
            rel_kind=kind.value,
            source_sheet=self.sheet,
            **context,
        )
        existing = self._by_key.get(link.key)
        if existing is None:
            self._by_key[link.key] = link
            self.stats.kinds[kind.value] += 1
        else:
            # Coalesce: fill attributes the first occurrence was missing.
            fills = {
                name: getattr(link, name)
                for name in self._CONTEXT_FIELDS
                if getattr(existing, name) is None and getattr(link, name) is not None
            }
            if fills:
                self._by_key[link.key] = replace(existing, **fills)
            self.stats.duplicate_links += 1

    @property
    def links(self) -> list[ParsedLink]:
        return list(self._by_key.values())


# ── Per-sheet parsers ─────────────────────────────────────────────────


def parse_main(ws, sheet: str) -> _Emitter:
    """Main: FRU | Part Number | Option | Opt-PN | Model | Assembly | Addl Sourcing |
    Manufactured by | Carrier | Alternate Carrier | Machine | Feature Code | Comment | Series."""
    em = _Emitter(sheet)
    for row in ws.iter_rows(min_row=2, values_only=True):
        row = _pad(row, 14)
        if not em.scan(row):
            continue
        fru = _pn(row[0])
        if not fru:
            em.stats.skipped_rows += 1
            continue
        fru_norm = normalize_mpn_key(fru)
        manufacturer = _clean(row[7])
        ctx = {
            "machine": _clean(row[10]),
            "series": _clean(row[13]),
            "note": _join_notes(f"FC {_clean(row[11])}" if _clean(row[11]) else None, _clean(row[12])),
        }
        em.add(fru, fru_norm, _pn(row[1]), FruLinkKind.IBM_11S, **ctx)
        em.add(fru, fru_norm, _pn(row[2]), FruLinkKind.OPTION, **ctx)
        em.add(fru, fru_norm, _pn(row[3]), FruLinkKind.OPTION_PN, **ctx)
        em.add(fru, fru_norm, _pn(row[4]), FruLinkKind.MFG_MODEL, manufacturer=manufacturer, **ctx)
        em.add(fru, fru_norm, _pn(row[5]), FruLinkKind.ASSEMBLY, **ctx)
        for pn in _split_pns(row[6]):
            em.add(fru, fru_norm, pn, FruLinkKind.SOURCING_PN, **ctx)
        for col, kind in ((8, FruLinkKind.TRAY), (9, FruLinkKind.TRAY_ALT)):
            pns, carrier_note = _parse_carrier(row[col])
            for pn in pns:
                em.add(fru, fru_norm, pn, kind, **{**ctx, "note": _join_notes(ctx["note"], carrier_note)})
    return em


def _parse_qual_sheet(ws, sheet: str, cols: dict[str, int | None], qual_override: str | None = None) -> _Emitter:
    """Shared parser for the qual-list sheets (Qlot / Gabor / CZ / CDC).

    ``cols`` maps logical fields → column index (None when the sheet lacks it).
    """
    em = _Emitter(sheet)
    width = max(c for c in cols.values() if c is not None) + 1

    def col(row, name):
        idx = cols.get(name)
        return row[idx] if idx is not None else None

    for row in ws.iter_rows(min_row=2, values_only=True):
        row = _pad(row, width)
        if not em.scan(row):
            continue
        fru = _pn(col(row, "fru"))
        if not fru:
            em.stats.skipped_rows += 1
            continue
        fru_norm = normalize_mpn_key(fru)
        oem = _clean(col(row, "oem"))
        machine = _clean(col(row, "brand")) or _clean(col(row, "type"))
        qual = qual_override or _clean(col(row, "qual"))
        qual_date = _date(col(row, "qual_date"))
        fru_desc = _clean(col(row, "fru_desc"))
        bare_desc = _clean(col(row, "bare_desc"))
        note = _join_notes(bare_desc, _clean(col(row, "comment")))
        em.add(
            fru,
            fru_norm,
            _pn(col(row, "drive_pn")),
            FruLinkKind.DRIVE_PN,
            manufacturer=oem,
            description=fru_desc,
            machine=machine,
            qual_status=qual,
            qual_date=qual_date,
            note=note,
        )
        em.add(
            fru,
            fru_norm,
            _pn(col(row, "model")),
            FruLinkKind.MFG_MODEL,
            manufacturer=oem,
            description=bare_desc,
            machine=machine,
            qual_status=qual,
            qual_date=qual_date,
        )
        em.add(fru, fru_norm, _pn(col(row, "tray")), FruLinkKind.TRAY, machine=machine)
    return em


def parse_qlot(ws, sheet: str) -> _Emitter:
    """Qlot as of 6.2025: FRU/DRIVE | FRU | Drivepn | Type | IW CSP qual | comment."""
    return _parse_qual_sheet(
        ws, sheet, {"fru": 1, "drive_pn": 2, "type": 3, "qual": 4, "comment": 5, "model": None, "tray": None}
    )


def parse_gabor(ws, sheet: str) -> _Emitter:
    """Gabor 11.13.25: ...

    | qual | date | Brand | Bare desc | OEM | FRU desc | Tray | Model | _ | CDC.
    """
    return _parse_qual_sheet(
        ws,
        sheet,
        {
            "fru": 1,
            "drive_pn": 2,
            "type": 3,
            "qual": 4,
            "qual_date": 5,
            "brand": 6,
            "bare_desc": 7,
            "oem": 8,
            "fru_desc": 9,
            "tray": 10,
            "model": 11,
        },
    )


def parse_cz(ws, sheet: str) -> _Emitter:
    """CZ ONLY Testing: like Gabor without Brand (qual is 'qlot approved - Only EMEA')."""
    return _parse_qual_sheet(
        ws,
        sheet,
        {
            "fru": 1,
            "drive_pn": 2,
            "type": 3,
            "qual": 4,
            "qual_date": 5,
            "oem": 6,
            "bare_desc": 7,
            "fru_desc": 8,
            "tray": 9,
            "model": 10,
        },
    )


def parse_cdc(ws, sheet: str) -> _Emitter:
    """CDC NOT yet AP Article Required: FRU | Drivepn | model(prose) | BRAND | CDC Platform.

    Rows are pending-qualification → qual_status "cdc_pending"; the prose model text
    becomes the description.
    """
    em = _Emitter(sheet)
    for row in ws.iter_rows(min_row=2, values_only=True):
        row = _pad(row, 6)
        if not em.scan(row):
            continue
        fru = _pn(row[1])
        if not fru:
            em.stats.skipped_rows += 1
            continue
        platform = _clean(row[5])
        em.add(
            fru,
            normalize_mpn_key(fru),
            _pn(row[2]),
            FruLinkKind.DRIVE_PN,
            description=_clean(row[3]),
            machine=_clean(row[4]),
            qual_status="cdc_pending",
            note=f"CDC platform: {platform}" if platform else None,
        )
    return em


def parse_lenovo_hdd(ws, sheet: str) -> _Emitter:
    """Lenovo-HDD: FRU | Part Number | Lenovo/Idea PN | Opt-PN | Model | ... | Manufactured by |
    Description | Carrier | Machine | FW | ... | Series."""
    em = _Emitter(sheet)
    for row in ws.iter_rows(min_row=2, values_only=True):
        row = _pad(row, 15)
        if not em.scan(row):
            continue
        fru = _pn(row[0])
        if not fru:
            em.stats.skipped_rows += 1
            continue
        fru_norm = normalize_mpn_key(fru)
        ctx = {"description": _clean(row[8]), "machine": _clean(row[10]), "series": _clean(row[14])}
        fw = _clean(row[11])
        em.add(fru, fru_norm, _pn(row[1]), FruLinkKind.IBM_11S, **ctx)
        em.add(fru, fru_norm, _pn(row[2]), FruLinkKind.LENOVO_PN, **ctx)
        em.add(
            fru,
            fru_norm,
            _pn(row[4]),
            FruLinkKind.MFG_MODEL,
            manufacturer=_clean(row[7]),
            note=f"FW {fw}" if fw else None,
            **ctx,
        )
        pns, carrier_note = _parse_carrier(row[9])
        for pn in pns:
            em.add(fru, fru_norm, pn, FruLinkKind.TRAY, note=carrier_note, **ctx)
    return em


def parse_lenovo_fru_pn(ws, sheet: str) -> _Emitter:
    """Lenovo FRU-PN: BOM Type | SBB Change Type | SBB | FRU | Qty | PPN | Last Modified.

    PPN values use the same SAP zero-padding as the FRU column ("0000000WG788" is
    "00WG788"), so the related side is de-padded for its norm too — raw keeps the
    original padded value.
    """
    em = _Emitter(sheet)
    for row in ws.iter_rows(min_row=2, values_only=True):
        row = _pad(row, 6)
        if not em.scan(row):
            continue
        fru_pair = _lenovo_fru(row[3])
        if not fru_pair:
            em.stats.skipped_rows += 1
            continue
        fru_raw, fru_norm = fru_pair
        ppn_pair = _lenovo_fru(row[5]) if _pn(row[5]) else None
        if ppn_pair:
            ppn_raw, ppn_norm = ppn_pair
            em.add(
                fru_raw, fru_norm, ppn_raw, FruLinkKind.LENOVO_PPN, note=_clean(row[0]), related_norm_override=ppn_norm
            )
    return em


def parse_lvn_vpd(ws, sheet: str) -> _Emitter:
    """LVN VPD Mapping: Brand | ... | Option P/N | FRU P/N | Option P/N | MFG Model |
    Make-to-Label P/N | Tray P/N | Description | ...

    Brand is a Lenovo platform label ("System x", "ThinkSystem") → note, not manufacturer;
    only a non-platform Brand would be treated as a manufacturer.
    """
    em = _Emitter(sheet)
    for row in ws.iter_rows(min_row=2, values_only=True):
        row = _pad(row, 11)
        if not em.scan(row):
            continue
        fru = _pn(row[5])
        if not fru:
            em.stats.skipped_rows += 1
            continue
        fru_norm = normalize_mpn_key(fru)
        brand = _clean(row[0])
        is_platform = brand is not None and brand.lower() in _LENOVO_PLATFORM_BRANDS
        manufacturer = brand if brand and not is_platform else None
        ctx = {
            "description": _clean(row[10]),
            "note": f"Brand: {brand}" if is_platform else None,
        }
        em.add(fru, fru_norm, _pn(row[4]), FruLinkKind.OPTION, **ctx)
        em.add(fru, fru_norm, _pn(row[6]), FruLinkKind.OPTION, **ctx)
        em.add(fru, fru_norm, _pn(row[7]), FruLinkKind.MFG_MODEL, manufacturer=manufacturer, **ctx)
        em.add(
            fru,
            fru_norm,
            _pn(row[8]),
            FruLinkKind.SOURCING_PN,
            description=ctx["description"],
            note=_join_notes("make-to-label", ctx["note"]),
        )
        em.add(fru, fru_norm, _pn(row[9]), FruLinkKind.TRAY, **ctx)
    return em


def parse_series(ws, sheet: str) -> _Emitter:
    """Series: fru | partno | idmodelo | Manufacturer | Capacity | Size | rpm | Pins |
    partnosg | bracket | bracket_alt | board | board_alt | screws | ... | Brand(series)."""
    em = _Emitter(sheet)
    for row in ws.iter_rows(min_row=2, values_only=True):
        row = _pad(row, 18)
        if not em.scan(row):
            continue
        fru = _pn(row[0])
        if not fru:
            em.stats.skipped_rows += 1
            continue
        fru_norm = normalize_mpn_key(fru)
        size_bits = [
            f"{_num(row[4])}GB" if _num(row[4]) else None,
            f"{_num(row[5])}in" if _num(row[5]) else None,
            f"{_num(row[6])}K" if _num(row[6]) else None,
            f"{_num(row[7])}pin" if _num(row[7]) else None,
        ]
        ctx = {"series": _clean(row[17]), "note": " ".join(b for b in size_bits if b) or None}
        em.add(fru, fru_norm, _pn(row[1]), FruLinkKind.IBM_11S, **ctx)
        em.add(fru, fru_norm, _pn(row[2]), FruLinkKind.MFG_MODEL, manufacturer=_clean(row[3]), **ctx)
        for col, kind in (
            (9, FruLinkKind.BRACKET),
            (10, FruLinkKind.BRACKET),
            (11, FruLinkKind.BOARD),
            (12, FruLinkKind.BOARD),
            (13, FruLinkKind.SCREWS),
        ):
            for pn in _split_pns(row[col]):
                em.add(fru, fru_norm, pn, kind, **ctx)
    return em


def parse_nseries(ws, sheet: str) -> _Emitter:
    """NSeries(NetApp): FRU | Description | 3X5 | Shuttle Type | Shuttle/Screw P/N |
    Dongle P/N | Dongle Screw P/N | Seagate MFG+FW | Hitachi MFG+FW | WD MFG+FW.

    Two header rows; a blank FRU cell continues the FRU above (forward-fill).
    """
    em = _Emitter(sheet)
    fru: str | None = None
    fru_norm = ""
    description: str | None = None
    for row in ws.iter_rows(min_row=3, values_only=True):
        row = _pad(row, 13)
        if not em.scan(row):
            continue
        row_fru = _pn(row[0])
        if row_fru:
            fru, fru_norm, description = row_fru, normalize_mpn_key(row_fru), _clean(row[1])
        if not fru:
            em.stats.skipped_rows += 1
            continue
        shuttle_type = _clean(row[3])
        for pn in _split_pns(row[4], seps=r"[/,;\n]"):
            em.add(
                fru,
                fru_norm,
                pn,
                FruLinkKind.SHUTTLE,
                description=description,
                note=f"Shuttle type: {shuttle_type}" if shuttle_type else None,
            )
        for pn in _split_pns(row[5], seps=r"[/,;\n]"):
            em.add(fru, fru_norm, pn, FruLinkKind.DONGLE, description=description)
        for pn in _split_pns(row[6], seps=r"[/,;\n]"):
            em.add(fru, fru_norm, pn, FruLinkKind.SCREWS, description=description)
        for pn_col, fw_col, mfr in ((7, 8, "Seagate"), (9, 10, "Hitachi"), (11, 12, "Western Digital")):
            fw = _clean(row[fw_col])
            for pn in _split_pns(row[pn_col], seps=r"[/,;\n]"):
                em.add(
                    fru,
                    fru_norm,
                    pn,
                    FruLinkKind.MFG_MODEL,
                    manufacturer=mfr,
                    description=description,
                    note=f"FW {fw}" if fw else None,
                )
    return em


PARSERS = [
    ("Main", parse_main),
    ("Qlot as of 6.2025", parse_qlot),
    ("Gabor 11.13.25", parse_gabor),
    ("CZ ONLY Testing", parse_cz),
    ("CDC NOT yet AP Article Required", parse_cdc),
    ("Lenovo-HDD", parse_lenovo_hdd),
    ("Lenovo FRU-PN", parse_lenovo_fru_pn),
    ("LVN VPD Mapping", parse_lvn_vpd),
    ("Series", parse_series),
    ("NSeries(NetApp)", parse_nseries),
]


def parse_workbook(path: str) -> tuple[list[ParsedLink], list[SheetStats]]:
    """Parse every mapped sheet of the workbook into deduplicated links + stats."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    links: list[ParsedLink] = []
    stats: list[SheetStats] = []
    try:
        for sheet_name, parser in PARSERS:
            if sheet_name not in wb.sheetnames:
                logger.warning("Sheet {!r} not found in workbook — skipping", sheet_name)
                continue
            emitter = parser(wb[sheet_name], sheet_name)
            links.extend(emitter.links)
            stats.append(emitter.stats)
    finally:
        wb.close()
    return links, stats


# ── Apply (chunked upsert) ────────────────────────────────────────────

_UPDATABLE = [
    "fru_raw",
    "related_raw",
    "manufacturer",
    "description",
    "series",
    "machine",
    "qual_status",
    "qual_date",
    "note",
]


def upsert_links(db, links: Iterable[ParsedLink], chunk_size: int = 1000) -> tuple[int, int]:
    """Insert new edges / refresh attributes on existing ones.

    Returns (inserted, updated).
    """
    from app.models import FruLink

    links = list(links)
    inserted = updated = 0
    for start in range(0, len(links), chunk_size):
        chunk = links[start : start + chunk_size]
        fru_norms = {link.fru_norm for link in chunk}
        existing = {
            (r.fru_norm, r.related_norm, r.rel_kind, r.source_sheet): r
            for r in db.query(FruLink).filter(FruLink.fru_norm.in_(fru_norms)).all()
        }
        to_insert = []
        for link in chunk:
            row = existing.get(link.key)
            if row is None:
                to_insert.append(
                    {
                        "fru_raw": link.fru_raw,
                        "fru_norm": link.fru_norm,
                        "related_raw": link.related_raw,
                        "related_norm": link.related_norm,
                        "rel_kind": link.rel_kind,
                        "source_sheet": link.source_sheet,
                        "manufacturer": link.manufacturer,
                        "description": link.description,
                        "series": link.series,
                        "machine": link.machine,
                        "qual_status": link.qual_status,
                        "qual_date": link.qual_date,
                        "note": link.note,
                    }
                )
            else:
                changed = False
                for attr in _UPDATABLE:
                    new = getattr(link, attr)
                    if new is not None and getattr(row, attr) != new:
                        setattr(row, attr, new)
                        changed = True
                if changed:
                    updated += 1
        if to_insert:
            db.execute(sa.insert(FruLink), to_insert)
            inserted += len(to_insert)
        db.commit()
    return inserted, updated


# ── CLI ───────────────────────────────────────────────────────────────


def _report(links: list[ParsedLink], stats: list[SheetStats]) -> None:
    for s in stats:
        kind_summary = ", ".join(f"{k}={n}" for k, n in sorted(s.kinds.items())) or "none"
        logger.info(
            "[{}] rows={} empty={} skipped={} dup_links={} links={} ({})",
            s.sheet,
            s.rows,
            s.empty_rows,
            s.skipped_rows,
            s.duplicate_links,
            sum(s.kinds.values()),
            kind_summary,
        )
    totals: Counter = Counter()
    for s in stats:
        totals.update(s.kinds)
    logger.info("TOTAL links={} by kind: {}", len(links), ", ".join(f"{k}={n}" for k, n in sorted(totals.items())))
    step = max(1, len(links) // 10)
    for link in links[::step][:10]:
        logger.info(
            "sample: [{}] {} -{}-> {} (mfr={}, qual={})",
            link.source_sheet,
            link.fru_raw,
            link.rel_kind,
            link.related_raw,
            link.manufacturer,
            link.qual_status,
        )


def main(path: str, apply: bool) -> None:
    logger.info("Parsing FRU matrix workbook: {} (mode={})", path, "APPLY" if apply else "dry-run")
    links, stats = parse_workbook(path)
    _report(links, stats)
    if not apply:
        logger.info("Dry run — nothing written. Re-run with --apply to upsert {} links.", len(links))
        return
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        inserted, updated = upsert_links(db, links)
        logger.info(
            "Upsert complete: {} inserted, {} updated, {} unchanged", inserted, updated, len(links) - inserted - updated
        )
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest the FRU_PN_TRAY matrix workbook into fru_links")
    parser.add_argument("xlsx", help="Path to the FRU matrix .xlsx workbook")
    parser.add_argument("--apply", action="store_true", help="Write to the database (default: dry run)")
    args = parser.parse_args()
    main(args.xlsx, apply=args.apply)

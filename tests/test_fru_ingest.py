"""Tests for the FRU matrix ingest command (app/management/ingest_fru_matrix.py).

What: Builds a miniature FRU_PN_TRAY workbook in-test with openpyxl (Main, Gabor,
      Lenovo FRU-PN, Series, NSeries sheets) covering the verified hygiene cases —
      \xa0 non-breaking spaces, "N/ A"/"#N/A"/PENDIENTE sentinels, comma multi-values,
      carrier parentheticals, prose cells, SAP zero-padded Lenovo FRUs, NSeries
      forward-fill — and asserts the parsed links. Also exercises the --apply upsert
      (insert new / refresh attributes / no duplicates).
Called by: pytest
Depends on: openpyxl, app/management/ingest_fru_matrix.py, app.models.FruLink
"""

import openpyxl
import pytest

from app.constants import FruLinkKind
from app.management.ingest_fru_matrix import (
    ParsedLink,
    _clean,
    _lenovo_fru,
    _parse_carrier,
    _pn,
    _split_pns,
    parse_workbook,
    upsert_links,
)
from app.models import FruLink


@pytest.fixture()
def mini_workbook(tmp_path):
    """A miniature FRU matrix workbook with the real sheet names/layouts."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    main = wb.create_sheet("Main")
    main.append(
        [
            "FRU",
            "Part Number",
            "Option",
            "Opt-PN",
            "Model",
            "Assembly",
            "Additional Sourcing Numbers",
            "Manufactured by",
            "Carrier",
            "Alternate Carrier",
            "Machine",
            "Feature Code",
            "Comment",
            "Series",
        ]
    )
    # Plain row: 11S + model + carrier with parenthetical.
    main.append(
        [
            "00AJ001",
            "68Y7789",
            "00AJ008",
            None,
            "SSDSC2BB120G4I",
            None,
            "40K1107, 42C0432",
            "Intel",
            "41Y0708 (w/interposer 41Y0709)",
            None,
            "x3650",
            "A2U4",
            "ThinkServer",
            "xSeries",
        ]
    )
    # Hygiene row: nbsp in PN, sentinel manufacturer, prose sourcing cell, slash carrier.
    main.append(
        [
            "00AJ002",
            "\xa0 68Y7790 ",
            "N/A",
            "#N/A",
            "WD4000FYYZ-88UL1B0",
            None,
            "Approved sub is 81Y9727 w/ same 11S PN AND FW SN03, but should be acceptable.",
            "PENDIENTE",
            "SM10G01157 / 00FC544 (Blue hot-swap)",
            "N/ A",
            None,
            None,
            None,
            "pSeries",
        ]
    )
    # Row with no usable FRU (prose) and a fully blank padding row.
    main.append(
        ["59Y5341 → rework in CZ", "12345AB", None, None, None, None, None, None, None, None, None, None, None, None]
    )
    main.append([None] * 14)

    gabor = wb.create_sheet("Gabor 11.13.25")
    gabor.append(
        [
            "FRU/DRIVE",
            "FRU",
            "Drivepn",
            "Type",
            "IW CSP qual",
            "Qlot approved date",
            "Brand",
            "Bare drive description",
            "OEM",
            "FRU Description",
            "Tray",
            "Drive Model",
            None,
            "CDC",
        ]
    )
    from datetime import datetime

    gabor.append(
        [
            "03GU64900VN562",
            "03GU649",
            "00VN562",
            "Storwize",
            "qlot approved",
            datetime(2024, 3, 14),
            "V5000 Gen2",
            "WD Paris C Non SED 18TB",
            "WDC",
            "18TB 3.5 HDD 7.2K SAS",
            "01LJ138",
            "WUH721818AL4200",
            None,
            "x",
        ]
    )
    # Drive Model #N/A → no mfg_model link.
    gabor.append(
        [
            "01AC60200VN225",
            "01AC602",
            "00VN225",
            "Storwize",
            "qlot approved",
            None,
            None,
            "SSD, Micron, S650DC",
            "Micron",
            "1.6TB 2.5 SSD SAS",
            "45W8687",
            "#N/A",
            None,
            None,
        ]
    )

    lfp = wb.create_sheet("Lenovo FRU-PN")
    lfp.append(["BOM Type", "SBB Change Type", "SBB", "FRU", "Qty", "PPN", "Last Modified Time"])
    lfp.append(["FRU-PPN", "", "", "0000000NV340_E00", "", "ESG0017964", ""])  # padded + suffix
    lfp.append(["FRU-PPN", "", "", "SB17B49754", "", "0000049Y4746", ""])  # 10-char FRU, padded PPN
    lfp.append(["FRU-PPN", "", "", "", "", "ESG999", ""])  # blank FRU → skipped

    series = wb.create_sheet("Series")
    series.append(
        [
            "fru",
            "partno",
            "idmodelo",
            "Manufacturer",
            "Capacity",
            "Size",
            "rpm x1000",
            "Pins",
            "partnosg",
            "bracket",
            "bracket_alt",
            "board",
            "board_alt",
            "screws",
            "wty",
            "cdc",
            "Alternate CDC-FRU",
            "Brand",
        ]
    )
    series.append(
        [
            "00P1517",
            "08K0263",
            "IC35L018UCDY10-0",
            "HITACHI/IBM",
            9,
            3.5,
            10,
            80,
            None,
            "U3X",
            "53P5457",
            "39J3525, 53P5456",
            None,
            "05J7985",
            60,
            0,
            None,
            "pSeries",
        ]
    )
    series.append(
        [
            "00P1518",
            "71P7530",
            "PENDIENTE",
            "SEAGATE",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "iSeries",
        ]
    )

    ns = wb.create_sheet("NSeries(NetApp)")
    ns.append(
        [
            "FRU",
            "Description",
            "3X5",
            "Shuttle Type",
            "Shuttle/",
            "Dongle P/N",
            "Dongle Screw P/N",
            "Seagate MFG P/N",
            "SG ",
            "Hitachi MFG P/N",
            "HIT FW",
            "Western Digital",
            "WD",
        ]
    )
    ns.append([None, None, None, None, "Screw P/N", None, None, None, "FW", None, None, None, "FW"])
    ns.append(
        [
            "45E1427",
            "HDD,FRU,1TB,7.2K RPM,SATA",
            "108-00183",
            "DS14MK2ATA",
            "X298A-R5, SP-298A-R5",
            "65695-02",
            "83315-xx",
            None,
            None,
            "0A35002 ",
            "A90A",
            "WD1002FBYS-05A6B0",
            "NA01",
        ]
    )
    # Continuation row: blank FRU → forward-filled from 45E1427.
    ns.append([None, None, None, None, None, None, None, "9JW154-038", "NA00", None, None, None, None])

    path = tmp_path / "mini_fru_matrix.xlsx"
    wb.save(path)
    return str(path)


@pytest.fixture()
def parsed(mini_workbook):
    links, stats = parse_workbook(mini_workbook)
    return links, {s.sheet: s for s in stats}


def _find(links, fru_norm=None, kind=None, related_norm=None):
    return [
        link
        for link in links
        if (fru_norm is None or link.fru_norm == fru_norm)
        and (kind is None or link.rel_kind == kind)
        and (related_norm is None or link.related_norm == related_norm)
    ]


class TestHygieneHelpers:
    def test_clean_strips_nbsp_and_collapses_whitespace(self):
        assert _clean("\xa0 SH20L07587 ") == "SH20L07587"
        assert _clean("a   b\xa0 c") == "a b c"

    @pytest.mark.parametrize("sentinel", ["N/ A", "N/A", "#N/A", "PENDIENTE", "None", "", "  ", "na", "Yes"])
    def test_clean_maps_sentinels_to_none(self, sentinel):
        assert _clean(sentinel) is None

    def test_pn_rejects_prose(self):
        assert _pn("Approved sub is 81Y9727 w/ same 11S PN") is None
        assert _pn("68Y7789") == "68Y7789"

    def test_split_pns_comma_multivalue(self):
        assert _split_pns("39J3525, 53P5456") == ["39J3525", "53P5456"]

    def test_parse_carrier_slash_and_parenthetical(self):
        pns, note = _parse_carrier("SM10G01157 / 00FC544 (Blue hot-swap)")
        assert pns == ["SM10G01157", "00FC544"]
        assert note == "Blue hot-swap"

    def test_lenovo_fru_depads_and_strips_suffix(self):
        assert _lenovo_fru("0000000NV340_E00") == ("0000000NV340_E00", "00nv340")
        # 10-char PNs without zero padding are untouched.
        assert _lenovo_fru("SB17B49754") == ("SB17B49754", "sb17b49754")
        assert _lenovo_fru("  ") is None


class TestMainSheet:
    def test_kind_mapping(self, parsed):
        links, _ = parsed
        assert _find(links, "00aj001", FruLinkKind.IBM_11S, "68y7789")
        assert _find(links, "00aj001", FruLinkKind.OPTION, "00aj008")
        model = _find(links, "00aj001", FruLinkKind.MFG_MODEL)
        assert model and model[0].manufacturer == "Intel"
        assert model[0].machine == "x3650"
        assert model[0].series == "xSeries"
        assert "FC A2U4" in model[0].note and "ThinkServer" in model[0].note

    def test_sourcing_numbers_split_and_prose_dropped(self, parsed):
        links, _ = parsed
        sourcing = _find(links, kind=FruLinkKind.SOURCING_PN)
        assert {link.related_raw for link in sourcing} == {"40K1107", "42C0432"}  # prose cell dropped

    def test_carrier_split_with_parenthetical_note(self, parsed):
        links, _ = parsed
        trays = _find(links, "00aj002", FruLinkKind.TRAY)
        assert {t.related_raw for t in trays} == {"SM10G01157", "00FC544"}
        assert all("Blue hot-swap" in t.note for t in trays)
        single = _find(links, "00aj001", FruLinkKind.TRAY)
        assert single[0].related_raw == "41Y0708"
        assert "w/interposer 41Y0709" in single[0].note

    def test_nbsp_and_sentinels(self, parsed):
        links, _ = parsed
        # "\xa0 68Y7790 " cleaned; PENDIENTE manufacturer nulled; N/A option dropped.
        assert _find(links, "00aj002", FruLinkKind.IBM_11S, "68y7790")
        assert _find(links, "00aj002", FruLinkKind.MFG_MODEL)[0].manufacturer is None
        assert not _find(links, "00aj002", FruLinkKind.OPTION)
        assert not _find(links, "00aj002", FruLinkKind.TRAY_ALT)  # "N/ A"

    def test_row_accounting(self, parsed):
        _, stats = parsed
        s = stats["Main"]
        assert s.rows == 4
        assert s.empty_rows == 1  # blank padding row
        assert s.skipped_rows == 1  # prose FRU


class TestGaborSheet:
    def test_drive_pn_carries_qual_context(self, parsed):
        links, _ = parsed
        drive = _find(links, "03gu649", FruLinkKind.DRIVE_PN, "00vn562")
        assert drive
        d = drive[0]
        assert d.qual_status == "qlot approved"
        assert str(d.qual_date) == "2024-03-14"
        assert d.manufacturer == "WDC"
        assert d.description == "18TB 3.5 HDD 7.2K SAS"
        assert d.machine == "V5000 Gen2"

    def test_tray_and_model_links(self, parsed):
        links, _ = parsed
        assert _find(links, "03gu649", FruLinkKind.TRAY, "01lj138")
        assert _find(links, "03gu649", FruLinkKind.MFG_MODEL, "wuh721818al4200")
        # Drive Model "#N/A" → no mfg_model link for the second FRU.
        assert not _find(links, "01ac602", FruLinkKind.MFG_MODEL)


class TestLenovoFruPnSheet:
    def test_fru_and_ppn_depadded(self, parsed):
        links, stats = parsed
        link = _find(links, "00nv340", FruLinkKind.LENOVO_PPN)[0]
        assert link.fru_raw == "0000000NV340_E00"  # raw keeps the padded original
        assert link.related_raw == "ESG0017964"
        assert link.note == "FRU-PPN"
        padded_ppn = _find(links, "sb17b49754", FruLinkKind.LENOVO_PPN)[0]
        assert padded_ppn.related_raw == "0000049Y4746"
        assert padded_ppn.related_norm == "49y4746"  # de-padded for joining
        assert stats["Lenovo FRU-PN"].skipped_rows == 1  # blank FRU


class TestSeriesSheet:
    def test_hardware_links_and_compact_note(self, parsed):
        links, _ = parsed
        boards = _find(links, "00p1517", FruLinkKind.BOARD)
        assert {b.related_raw for b in boards} == {"39J3525", "53P5456"}  # comma split
        brackets = _find(links, "00p1517", FruLinkKind.BRACKET)
        assert {b.related_raw for b in brackets} == {"U3X", "53P5457"}
        assert _find(links, "00p1517", FruLinkKind.SCREWS, "05j7985")
        model = _find(links, "00p1517", FruLinkKind.MFG_MODEL)[0]
        assert model.manufacturer == "HITACHI/IBM"
        assert model.note == "9GB 3.5in 10K 80pin"
        assert model.series == "pSeries"

    def test_pendiente_model_skipped(self, parsed):
        links, _ = parsed
        assert not _find(links, "00p1518", FruLinkKind.MFG_MODEL)
        assert _find(links, "00p1518", FruLinkKind.IBM_11S, "71p7530")


class TestNSeriesSheet:
    def test_vendor_models_with_fw_notes(self, parsed):
        links, _ = parsed
        hitachi = _find(links, "45e1427", FruLinkKind.MFG_MODEL, "0a35002")[0]
        assert hitachi.manufacturer == "Hitachi"
        assert hitachi.note == "FW A90A"
        wd = _find(links, "45e1427", FruLinkKind.MFG_MODEL, "wd1002fbys05a6b0")[0]
        assert wd.manufacturer == "Western Digital"

    def test_forward_fill_continuation_row(self, parsed):
        links, _ = parsed
        seagate = _find(links, "45e1427", FruLinkKind.MFG_MODEL, "9jw154038")
        assert seagate and seagate[0].manufacturer == "Seagate"
        assert seagate[0].note == "FW NA00"

    def test_shuttle_dongle_screws(self, parsed):
        links, _ = parsed
        shuttles = _find(links, "45e1427", FruLinkKind.SHUTTLE)
        assert {s.related_raw for s in shuttles} == {"X298A-R5", "SP-298A-R5"}
        assert all("Shuttle type: DS14MK2ATA" in s.note for s in shuttles)
        assert _find(links, "45e1427", FruLinkKind.DONGLE, "6569502")
        assert _find(links, "45e1427", FruLinkKind.SCREWS, "83315xx")


class TestUpsert:
    def _link(self, **overrides):
        base = dict(
            fru_raw="00AJ001",
            fru_norm="00aj001",
            related_raw="68Y7789",
            related_norm="68y7789",
            rel_kind=FruLinkKind.IBM_11S.value,
            source_sheet="Main",
        )
        base.update(overrides)
        return ParsedLink(**base)

    def test_insert_then_idempotent_reapply(self, db_session, mini_workbook):
        links, _ = parse_workbook(mini_workbook)
        inserted, updated = upsert_links(db_session, links, chunk_size=7)
        assert inserted == len(links)
        assert updated == 0
        assert db_session.query(FruLink).count() == len(links)

        inserted2, updated2 = upsert_links(db_session, links, chunk_size=7)
        assert inserted2 == 0
        assert updated2 == 0
        assert db_session.query(FruLink).count() == len(links)

    def test_update_refreshes_attributes(self, db_session):
        upsert_links(db_session, [self._link()])
        row = db_session.query(FruLink).one()
        assert row.manufacturer is None

        inserted, updated = upsert_links(db_session, [self._link(manufacturer="Intel", note="FC A2U4")])
        assert (inserted, updated) == (0, 1)
        row = db_session.query(FruLink).one()
        assert row.manufacturer == "Intel"
        assert row.note == "FC A2U4"

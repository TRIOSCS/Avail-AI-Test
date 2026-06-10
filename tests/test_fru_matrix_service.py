"""Tests for app/services/fru_matrix_service.py (forward + reverse FRU views).

What: Seeds fru_links rows and asserts get_fru_view section grouping, cross-sheet
      dedup (richer rows preferred, missing attributes coalesced), qualified-first
      ordering, raw-input normalization, and get_reverse_view dedup/context.
Called by: pytest
Depends on: app.models.FruLink, app.services.fru_matrix_service
"""

from datetime import date

import pytest

from app.constants import FruLinkKind
from app.models import FruLink
from app.services.fru_matrix_service import get_fru_view, get_reverse_view


def _add(db, fru="00AJ001", related="68Y7789", kind=FruLinkKind.IBM_11S, sheet="Main", **attrs):
    link = FruLink(
        fru_raw=fru,
        fru_norm=fru.replace("-", "").lower(),
        related_raw=related,
        related_norm=related.replace("-", "").lower(),
        rel_kind=kind.value,
        source_sheet=sheet,
        **attrs,
    )
    db.add(link)
    db.commit()
    return link


class TestGetFruView:
    def test_none_for_unknown_or_blank(self, db_session):
        assert get_fru_view(db_session, "NOPE123") is None
        assert get_fru_view(db_session, "") is None

    def test_normalizes_raw_input(self, db_session):
        _add(db_session)
        for query in ["00AJ001", "00aj001", " 00-AJ-001 "]:
            view = get_fru_view(db_session, query)
            assert view is not None
            assert view.fru_raw == "00AJ001"
            assert view.fru_norm == "00aj001"

    def test_sections_grouped_by_kind(self, db_session):
        _add(db_session, related="68Y7789", kind=FruLinkKind.IBM_11S)
        _add(db_session, related="ST9300603SS", kind=FruLinkKind.MFG_MODEL, manufacturer="Seagate")
        _add(db_session, related="44T2216", kind=FruLinkKind.TRAY)
        _add(db_session, related="39J3525", kind=FruLinkKind.BOARD)
        _add(db_session, related="00AJ008", kind=FruLinkKind.OPTION)

        view = get_fru_view(db_session, "00AJ001")
        labels = [s.label for s in view.sections]
        assert labels == ["Approved drives & models", "11S part numbers", "Options", "Trays & hardware"]
        hardware = view.sections[3]
        assert {i.related_raw for i in hardware.items} == {"44T2216", "39J3525"}
        assert view.total_links == 5

    def test_dedup_across_sheets_prefers_rich_row_and_coalesces(self, db_session):
        # Same drive under the same FRU from two sheets: Gabor row carries qual data,
        # Main row carries the description — the merged item must have both.
        _add(db_session, related="00VN562", kind=FruLinkKind.DRIVE_PN, sheet="Main", description="18TB HDD")
        _add(
            db_session,
            related="00VN562",
            kind=FruLinkKind.DRIVE_PN,
            sheet="Gabor 11.13.25",
            manufacturer="WDC",
            qual_status="qlot approved",
            qual_date=date(2024, 3, 14),
        )
        view = get_fru_view(db_session, "00AJ001")
        items = view.sections[0].items
        assert len(items) == 1
        item = items[0]
        assert item.qual_status == "qlot approved"
        assert item.qual_date == date(2024, 3, 14)
        assert item.manufacturer == "WDC"
        assert item.description == "18TB HDD"
        assert set(item.source_sheets) == {"Main", "Gabor 11.13.25"}

    def test_qualified_items_sort_first(self, db_session):
        _add(db_session, related="AAA111", kind=FruLinkKind.DRIVE_PN)
        _add(db_session, related="ZZZ999", kind=FruLinkKind.DRIVE_PN, qual_status="qlot approved")
        view = get_fru_view(db_session, "00AJ001")
        assert [i.related_raw for i in view.sections[0].items] == ["ZZZ999", "AAA111"]

    def test_series_and_machine_context(self, db_session):
        _add(db_session, related="68Y7789", series="xSeries", machine="x3650")
        _add(db_session, related="44T2216", kind=FruLinkKind.TRAY, series="xSeries", machine="x3550")
        view = get_fru_view(db_session, "00AJ001")
        assert view.series == ("xSeries",)
        assert set(view.machines) == {"x3650", "x3550"}


class TestGetReverseView:
    def test_empty_for_unknown_or_blank(self, db_session):
        assert get_reverse_view(db_session, "NOPE123") == []
        assert get_reverse_view(db_session, "") == []

    def test_finds_frus_for_related_pn(self, db_session):
        _add(db_session, fru="00AJ001", related="ST9300603SS", kind=FruLinkKind.MFG_MODEL, manufacturer="Seagate")
        _add(db_session, fru="42D0638", related="ST9300603SS", kind=FruLinkKind.MFG_MODEL)
        usages = get_reverse_view(db_session, "st9300603ss")
        assert [u.fru_raw for u in usages] == ["00AJ001", "42D0638"]
        assert usages[0].kind_label == "Manufacturer model"
        assert usages[0].manufacturer == "Seagate"

    def test_dedups_same_fru_kind_across_sheets(self, db_session):
        _add(db_session, fru="00AJ001", related="00VN562", kind=FruLinkKind.DRIVE_PN, sheet="Main")
        _add(
            db_session,
            fru="00AJ001",
            related="00VN562",
            kind=FruLinkKind.DRIVE_PN,
            sheet="Qlot as of 6.2025",
            qual_status="qlot approved",
        )
        usages = get_reverse_view(db_session, "00VN562")
        assert len(usages) == 1
        assert usages[0].qual_status == "qlot approved"  # preferred from the richer row

    def test_same_pn_different_roles_kept(self, db_session):
        _add(db_session, fru="00AJ001", related="44T2216", kind=FruLinkKind.TRAY)
        _add(db_session, fru="00AJ001", related="44T2216", kind=FruLinkKind.TRAY_ALT)
        usages = get_reverse_view(db_session, "44T2216")
        assert {u.rel_kind for u in usages} == {FruLinkKind.TRAY.value, FruLinkKind.TRAY_ALT.value}


class TestModelValidation:
    def test_rel_kind_validator_rejects_unknown(self, db_session):
        with pytest.raises(ValueError):
            FruLink(
                fru_raw="A",
                fru_norm="a11",
                related_raw="B11",
                related_norm="b11",
                rel_kind="not_a_kind",
                source_sheet="Main",
            )

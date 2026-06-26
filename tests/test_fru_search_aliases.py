"""FRU alias expansion into supplier search (optimization plan 2026-06-12 item 2.7).

What: get_search_aliases (both lookup directions, alias-kind filter, per-norm
      dedup with shortest-raw display + manufacturer coalescing, priority
      ordering), search_service._expand_fru_aliases (cap 8, dedup vs primary +
      explicit substitutes, MAX_SUBSTITUTES room, provenance tag),
      search_requirement integration (aliases reach the connector fan-out and
      persist as system-derived substitutes on both the full path and the
      all-cached short-circuit; no-links path unchanged),
      parse_substitute_mpns source-key preservation, and the |fru_alias_mpns
      template filter behind the "via FRU crosswalk" tooltip.
Called by: pytest
Depends on: app.search_service, app.services.fru_matrix_service,
            app.models.FruLink/Requirement/Requisition/MaterialCard,
            app.utils.normalization, app.template_env
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import FRU_ALIAS_SOURCE, FruLinkKind
from app.models import FruLink, MaterialCard, Requirement, Requisition
from app.search_service import (
    MAX_FRU_ALIASES,
    _expand_fru_aliases,
    get_all_pns,
    search_requirement,
)
from app.services.fru_matrix_service import SEARCH_ALIAS_KINDS, get_search_aliases
from app.template_env import _fru_alias_mpns_filter
from app.utils.normalization import MAX_SUBSTITUTES, normalize_mpn_key, parse_substitute_mpns


def _link(db: Session, fru: str, related: str, kind: FruLinkKind, sheet: str = "Main", **attrs) -> FruLink:
    link = FruLink(
        fru_raw=fru,
        fru_norm=normalize_mpn_key(fru),
        related_raw=related,
        related_norm=normalize_mpn_key(related),
        rel_kind=kind.value,
        source_sheet=sheet,
        **attrs,
    )
    db.add(link)
    db.flush()
    return link


def _make_requirement(db: Session, user, primary_mpn: str, substitutes=None) -> Requirement:
    requisition = Requisition(
        name="REQ-FRU-1",
        customer_name="Test Co",
        status="open",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(requisition)
    db.flush()
    req = Requirement(
        requisition_id=requisition.id,
        primary_mpn=primary_mpn,
        substitutes=substitutes or [],
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.commit()
    return req


def _patch_fetch_fresh():
    """Patch the supplier fan-out to return no offers/sightings."""
    return patch("app.search_service._fetch_fresh", new=AsyncMock(return_value=([], [])))


class TestGetSearchAliases:
    def test_blank_input_returns_empty(self, db_session):
        assert get_search_aliases(db_session, "") == []
        assert get_search_aliases(db_session, "  ") == []

    def test_no_links_returns_empty(self, db_session):
        assert get_search_aliases(db_session, "00AJ001") == []

    def test_forward_direction_alias_kinds_only(self, db_session):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL, manufacturer="Seagate")
        _link(db_session, "00AJ001", "9RZ268-039", FruLinkKind.DRIVE_PN)
        _link(db_session, "00AJ001", "00AJ004", FruLinkKind.OPTION)
        _link(db_session, "00AJ001", "11S00AJ001", FruLinkKind.IBM_11S)
        # Non-alias kinds must NOT expand into supplier queries.
        _link(db_session, "00AJ001", "TRAY9999", FruLinkKind.TRAY)
        _link(db_session, "00AJ001", "PPN12345", FruLinkKind.LENOVO_PPN)
        db_session.commit()

        aliases = get_search_aliases(db_session, "00AJ001")

        assert [a.mpn for a in aliases] == ["ST91000640NS", "9RZ268-039", "00AJ004", "11S00AJ001"]
        assert aliases[0].manufacturer == "Seagate"
        assert {a.rel_kind for a in aliases} == {k.value for k in SEARCH_ALIAS_KINDS}

    def test_reverse_direction_returns_fru(self, db_session):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL, manufacturer="Seagate")
        db_session.commit()

        aliases = get_search_aliases(db_session, "ST91000640NS")

        assert [a.mpn for a in aliases] == ["00AJ001"]
        # FruLink.manufacturer describes the related part (the searched MPN
        # here), not the FRU — reverse hits carry no manufacturer claim.
        assert aliases[0].manufacturer == ""

    def test_input_is_normalized(self, db_session):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL)
        db_session.commit()

        assert [a.mpn for a in get_search_aliases(db_session, " 00aj-001 ")] == ["ST91000640NS"]

    def test_priority_ordering_across_kinds(self, db_session):
        # Inserted in reverse priority order — output must still rank
        # mfg_model, drive_pn, option, ibm_11s.
        _link(db_session, "00AJ001", "11S00AJ001", FruLinkKind.IBM_11S)
        _link(db_session, "00AJ001", "00AJ004", FruLinkKind.OPTION)
        _link(db_session, "00AJ001", "9RZ268-039", FruLinkKind.DRIVE_PN)
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL)
        db_session.commit()

        kinds = [a.rel_kind for a in get_search_aliases(db_session, "00AJ001")]

        assert kinds == [
            FruLinkKind.MFG_MODEL.value,
            FruLinkKind.DRIVE_PN.value,
            FruLinkKind.OPTION.value,
            FruLinkKind.IBM_11S.value,
        ]

    def test_dedup_per_norm_prefers_shortest_raw_and_coalesces_manufacturer(self, db_session):
        # Same alias under two sheets with raw spellings sharing one norm key;
        # the shortest spelling wins, manufacturer fills from the richer row.
        _link(db_session, "00AJ001", "68Y-7789", FruLinkKind.MFG_MODEL, sheet="A")
        _link(db_session, "00AJ001", "68Y7789", FruLinkKind.MFG_MODEL, sheet="B", manufacturer="IBM")
        db_session.commit()

        aliases = get_search_aliases(db_session, "00AJ001")

        assert len(aliases) == 1
        assert aliases[0].mpn == "68Y7789"
        assert aliases[0].manufacturer == "IBM"


class TestExpandFruAliases:
    def test_no_links_returns_empty(self, db_session, test_user):
        req = _make_requirement(db_session, test_user, "00AJ001")
        assert _expand_fru_aliases(db_session, req) == []

    def test_alias_dict_format_and_provenance(self, db_session, test_user):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL, manufacturer="Seagate")
        req = _make_requirement(db_session, test_user, "00AJ001")

        aliases = _expand_fru_aliases(db_session, req)

        assert aliases == [{"mpn": "ST91000640NS", "manufacturer": "Seagate", "source": FRU_ALIAS_SOURCE}]

    def test_caps_at_eight_in_priority_order(self, db_session, test_user):
        for i in range(6):
            _link(db_session, "00AJ001", f"MODEL-{i}00", FruLinkKind.MFG_MODEL)
        for i in range(4):
            _link(db_session, "00AJ001", f"DRIVE-{i}00", FruLinkKind.DRIVE_PN)
        req = _make_requirement(db_session, test_user, "00AJ001")

        aliases = _expand_fru_aliases(db_session, req)

        assert len(aliases) == MAX_FRU_ALIASES == 8
        # All 6 mfg_model aliases survive the cap; drive_pn fills the rest.
        assert [a["mpn"] for a in aliases[:6]] == [f"MODEL-{i}00" for i in range(6)]
        assert all(a["source"] == FRU_ALIAS_SOURCE for a in aliases)

    def test_dedup_against_primary_and_explicit_substitutes(self, db_session, test_user):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL)
        _link(db_session, "00AJ001", "9RZ268-039", FruLinkKind.DRIVE_PN)
        # Explicit substitute matches the first alias by canonical key
        # (case + dash variations must not escape the dedup).
        req = _make_requirement(
            db_session,
            test_user,
            "00AJ001",
            substitutes=[{"mpn": "st9-1000640ns", "manufacturer": ""}],
        )

        aliases = _expand_fru_aliases(db_session, req)

        assert [a["mpn"] for a in aliases] == ["9RZ268-039"]

    def test_no_room_when_substitutes_at_global_cap(self, db_session, test_user):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL)
        subs = [{"mpn": f"SUB-{i:03d}", "manufacturer": ""} for i in range(MAX_SUBSTITUTES)]
        req = _make_requirement(db_session, test_user, "00AJ001", substitutes=subs)

        assert _expand_fru_aliases(db_session, req) == []

    def test_blank_primary_returns_empty(self, db_session, test_user):
        req = _make_requirement(db_session, test_user, "")
        assert _expand_fru_aliases(db_session, req) == []


class TestSearchRequirementFruAliases:
    """Integration: aliases flow into the connector fan-out and persist as
    system-derived substitutes."""

    @pytest.fixture(autouse=True)
    def _disable_spec_resolver(self, monkeypatch):
        monkeypatch.setattr(settings, "spec_resolver_enabled", False)

    async def test_forward_alias_injected_and_persisted(self, db_session, test_user):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL, manufacturer="Seagate")
        req = _make_requirement(db_session, test_user, "00AJ001")

        with _patch_fetch_fresh() as fetch_mock:
            result = await search_requirement(req, db_session)

        # Alias reaches the supplier fan-out alongside the primary.
        assert fetch_mock.call_args[0][0] == ["00AJ001", "ST91000640NS"]
        assert result["mpn_results"] == {"00AJ001": "searched", "ST91000640NS": "searched"}

        # Alias persisted as a system-derived substitute with provenance.
        db_session.expire(req)
        assert req.substitutes == [{"mpn": "ST91000640NS", "manufacturer": "Seagate", "source": FRU_ALIAS_SOURCE}]
        # Future searches pick it up through the normal primary+subs contract.
        assert get_all_pns(req) == ["00AJ001", "ST91000640NS"]

    async def test_reverse_alias_injected(self, db_session, test_user):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL, manufacturer="Seagate")
        req = _make_requirement(db_session, test_user, "ST91000640NS")

        with _patch_fetch_fresh() as fetch_mock:
            await search_requirement(req, db_session)

        assert fetch_mock.call_args[0][0] == ["ST91000640NS", "00AJ001"]
        db_session.expire(req)
        assert req.substitutes == [{"mpn": "00AJ001", "manufacturer": "", "source": FRU_ALIAS_SOURCE}]

    async def test_no_links_path_unchanged(self, db_session, test_user):
        req = _make_requirement(db_session, test_user, "LM317T")

        with _patch_fetch_fresh() as fetch_mock:
            result = await search_requirement(req, db_session)

        assert fetch_mock.call_args[0][0] == ["LM317T"]
        assert result["mpn_results"] == {"LM317T": "searched"}
        db_session.expire(req)
        assert req.substitutes == []

    async def test_alias_matching_explicit_substitute_not_duplicated(self, db_session, test_user):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL)
        req = _make_requirement(
            db_session,
            test_user,
            "00AJ001",
            substitutes=[{"mpn": "ST91000640NS", "manufacturer": "Seagate"}],
        )

        with _patch_fetch_fresh() as fetch_mock:
            await search_requirement(req, db_session)

        assert fetch_mock.call_args[0][0] == ["00AJ001", "ST91000640NS"]
        db_session.expire(req)
        # Explicit substitute untouched — no duplicate, no provenance rewrite.
        assert req.substitutes == [{"mpn": "ST91000640NS", "manufacturer": "Seagate"}]

    async def test_short_circuit_path_still_persists_aliases(self, db_session, test_user):
        """Every MPN within cooldown including the alias → no connector calls, but the
        system-derived substitute still lands on the requirement."""
        now = datetime.now(timezone.utc)
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL, manufacturer="Seagate")
        for mpn in ("00AJ001", "ST91000640NS"):
            db_session.add(
                MaterialCard(
                    normalized_mpn=normalize_mpn_key(mpn),
                    display_mpn=mpn,
                    last_searched_at=now - timedelta(hours=12),
                )
            )
        req = _make_requirement(db_session, test_user, "00AJ001")

        with _patch_fetch_fresh() as fetch_mock:
            result = await search_requirement(req, db_session)

        assert fetch_mock.call_count == 0
        assert result["mpn_results"] == {"00AJ001": "cached", "ST91000640NS": "cached"}
        db_session.expire(req)
        assert req.substitutes == [{"mpn": "ST91000640NS", "manufacturer": "Seagate", "source": FRU_ALIAS_SOURCE}]

    async def test_second_search_does_not_duplicate_aliases(self, db_session, test_user):
        _link(db_session, "00AJ001", "ST91000640NS", FruLinkKind.MFG_MODEL)
        req = _make_requirement(db_session, test_user, "00AJ001")

        with _patch_fetch_fresh():
            await search_requirement(req, db_session)
            db_session.expire(req)
            await search_requirement(req, db_session)

        db_session.expire(req)
        assert [s["mpn"] for s in req.substitutes] == ["ST91000640NS"]


class TestParseSubstituteMpnsSourcePreservation:
    def test_source_key_preserved(self):
        subs = [{"mpn": "ST91000640NS", "manufacturer": "Seagate", "source": FRU_ALIAS_SOURCE}]
        result = parse_substitute_mpns(subs, "00AJ001")
        assert result == [{"mpn": "ST91000640NS", "manufacturer": "Seagate", "source": FRU_ALIAS_SOURCE}]

    def test_no_source_key_added_when_absent(self):
        result = parse_substitute_mpns([{"mpn": "LM317T", "manufacturer": "TI"}], "PRIMARY")
        assert result == [{"mpn": "LM317T", "manufacturer": "TI"}]

    def test_blank_source_dropped(self):
        result = parse_substitute_mpns([{"mpn": "LM317T", "manufacturer": "", "source": "  "}], "PRIMARY")
        assert "source" not in result[0]


class TestFruAliasMpnsFilter:
    def test_returns_normalized_crosswalk_mpns_only(self):
        subs = [
            {"mpn": "st91000640ns", "manufacturer": "Seagate", "source": FRU_ALIAS_SOURCE},
            {"mpn": "LM317T", "manufacturer": "TI"},  # explicit sub — no tooltip
            "PLAINSTRING1",  # legacy string form — no provenance possible
            {"mpn": "OTHER123", "source": "something_else"},
        ]
        assert _fru_alias_mpns_filter(subs) == {"ST91000640NS"}

    def test_empty_and_none(self):
        assert _fru_alias_mpns_filter(None) == set()
        assert _fru_alias_mpns_filter([]) == set()

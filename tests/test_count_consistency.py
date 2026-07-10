"""Sidebar count consistency — counts must equal what the results list would show.

Covers PR-C of the filter truth pack:
- get_facet_counts / get_global_facet_counts narrow under the full card-level filter
  set (q, brand, statuses, lifecycle, rohs, condition, sourcing flags) via the shared
  _apply_card_filters builder, so sidebar counts can never overstate vs the list.
- Self-exclusion semantics survive: a facet's own selection never narrows its own
  counts (spec facets via pass 2; global facets via own-key drop).
- The deleted_at IS NULL guard is always applied to count queries, and facet rows
  whose card was re-categorized since the facet was written produce no phantom counts.
- displays.resolution seed vocabulary is panel resolutions (the character-LCD enum is
  gone) and reseed_changed_schemas reconciles a drifted live row.

Depends on: conftest.py (db_session/client), MaterialCard/MaterialSpecFacet models,
faceted_search_service, commodity_registry seeds.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import COMMODITY_SPEC_SEEDS, reseed_changed_schemas
from app.services.faceted_search_service import (
    get_facet_counts,
    get_global_facet_counts,
    search_materials_faceted,
)
from tests.conftest import engine  # noqa: F401


def _card(
    db: Session,
    mpn: str,
    *,
    category: str = "hdd",
    interface: str | None = None,
    form_factor: str | None = None,
    description: str | None = None,
    manufacturer: str | None = None,
    brand: str | None = None,
    lifecycle_status: str | None = None,
    condition: str | None = None,
    enrichment_status: str = "unenriched",
    datasheet_url: str | None = None,
    deleted_at: datetime | None = None,
) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn,
        display_mpn=mpn.upper(),
        category=category,
        description=description,
        manufacturer=manufacturer,
        brand=brand,
        lifecycle_status=lifecycle_status,
        condition=condition,
        enrichment_status=enrichment_status,
        datasheet_url=datasheet_url,
        deleted_at=deleted_at,
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    for spec_key, value in (("interface", interface), ("form_factor", form_factor)):
        if value is not None:
            db.add(MaterialSpecFacet(material_card_id=card.id, category="hdd", spec_key=spec_key, value_text=value))
    db.flush()
    return card


# ── get_facet_counts: card-level narrowing ──────────────────────────────


def test_facet_counts_narrow_under_q(db_session: Session):
    _card(db_session, "st4000", interface="SATA", form_factor='3.5"', description="ENTERPRISE DRIVE")
    _card(db_session, "wd2000", interface="SAS", form_factor='2.5"', description="MOBILE DRIVE")
    db_session.commit()

    counts = get_facet_counts(db_session, "hdd", card_filters={"q": "st4000"})
    assert counts["interface"] == {"SATA": 1}
    assert counts["form_factor"] == {'3.5"': 1}


def test_facet_counts_narrow_under_brand(db_session: Session):
    _card(db_session, "d1", interface="SATA", manufacturer="Seagate Technology")
    _card(db_session, "d2", interface="SAS", brand="IBM")
    _card(db_session, "d3", interface="SCSI", manufacturer="Western Digital")
    db_session.commit()

    # Dual-brand OR: matches manufacturer (d1) and brand (d2), not d3.
    counts = get_facet_counts(db_session, "hdd", card_filters={"manufacturers": ["Seagate Technology", "IBM"]})
    assert counts["interface"] == {"SATA": 1, "SAS": 1}


def test_facet_counts_narrow_under_global_filters(db_session: Session):
    _card(db_session, "d1", interface="SATA", lifecycle_status="active", enrichment_status="verified")
    _card(db_session, "d2", interface="SAS", lifecycle_status="eol", enrichment_status="verified")
    _card(db_session, "d3", interface="SCSI", lifecycle_status="active")
    db_session.commit()

    counts = get_facet_counts(db_session, "hdd", card_filters={"lifecycle": ["active"], "statuses": ["verified"]})
    assert counts["interface"] == {"SATA": 1}


def test_facet_counts_exclude_deleted_cards_without_card_filters(db_session: Session):
    _card(db_session, "live1", interface="SATA")
    _card(db_session, "gone1", interface="SATA", deleted_at=datetime.now(UTC))
    db_session.commit()

    # The deleted_at guard applies even with NO active card filters.
    counts = get_facet_counts(db_session, "hdd")
    assert counts["interface"] == {"SATA": 1}


def test_facet_counts_exclude_recategorized_cards(db_session: Session):
    # Facet row says hdd, but the card has since been re-categorized — no phantom count.
    drifted = _card(db_session, "drift1", interface="SATA")
    drifted.category = "ssd"
    _card(db_session, "still1", interface="SATA")
    db_session.commit()

    counts = get_facet_counts(db_session, "hdd")
    assert counts["interface"] == {"SATA": 1}


def test_facet_self_exclusion_survives_card_filters(db_session: Session):
    _card(db_session, "d1", interface="SATA", form_factor='3.5"', description="RACK DRIVE")
    _card(db_session, "d2", interface="SAS", form_factor='2.5"', description="RACK DRIVE")
    _card(db_session, "d3", interface="SCSI", form_factor='3.5"', description="DESKTOP DRIVE")
    db_session.commit()

    counts = get_facet_counts(
        db_session,
        "hdd",
        active_filters={"interface": ["SATA"]},
        card_filters={"q": "RACK"},
    )
    # interface (actively filtered) self-excludes its own selection but still honors
    # the card-level q — d3 (DESKTOP) stays out, d2 (SAS, RACK) stays in.
    assert counts["interface"] == {"SATA": 1, "SAS": 1}
    # form_factor is narrowed by interface=SATA AND q=RACK → only d1.
    assert counts["form_factor"] == {'3.5"': 1}


def test_facet_counts_ignore_stray_sub_filters_in_card_filters(db_session: Session):
    _card(db_session, "d1", interface="SATA")
    _card(db_session, "d2", interface="SAS")
    db_session.commit()

    # A stray sub_filters key inside card_filters must not double-narrow pass 2
    # (self-exclusion would silently break) — it is stripped, spec narrowing only
    # rides active_filters.
    counts = get_facet_counts(
        db_session,
        "hdd",
        active_filters={"interface": ["SATA"]},
        card_filters={"sub_filters": {"interface": ["SATA"]}, "commodity": "ssd"},
    )
    assert counts["interface"] == {"SATA": 1, "SAS": 1}


def test_facet_count_equals_list_total_for_same_filters(db_session: Session):
    """The consistency contract: a facet value's count == the list total after
    selecting it, under the same active card-level filters."""
    _card(db_session, "d1", interface="SATA", lifecycle_status="active", description="RACK DRIVE")
    _card(db_session, "d2", interface="SATA", lifecycle_status="eol", description="RACK DRIVE")
    _card(db_session, "d3", interface="SAS", lifecycle_status="active", description="RACK DRIVE")
    db_session.commit()

    card_filters = {"q": "RACK", "lifecycle": ["active"]}
    counts = get_facet_counts(db_session, "hdd", card_filters=card_filters)

    _, total = search_materials_faceted(
        db_session, commodity="hdd", sub_filters={"interface": ["SATA"]}, **card_filters
    )
    assert counts["interface"]["SATA"] == total == 1


# ── get_global_facet_counts: narrowing + self-exclusion ─────────────────


def test_global_counts_narrow_under_q_and_brand(db_session: Session):
    _card(db_session, "d1", lifecycle_status="active", manufacturer="Seagate Technology", description="RACK DRIVE")
    _card(db_session, "d2", lifecycle_status="active", manufacturer="Western Digital", description="RACK DRIVE")
    _card(db_session, "d3", lifecycle_status="eol", manufacturer="Seagate Technology", description="DESKTOP DRIVE")
    db_session.commit()

    counts = get_global_facet_counts(db_session, filters={"q": "RACK", "manufacturers": ["Seagate Technology"]})
    assert counts["lifecycle"] == {"active": 1}


def test_global_counts_self_exclude_own_facet(db_session: Session):
    _card(db_session, "d1", lifecycle_status="active", condition="New")
    _card(db_session, "d2", lifecycle_status="eol", condition="Pulled")
    db_session.commit()

    counts = get_global_facet_counts(db_session, filters={"lifecycle": ["active"]})
    # lifecycle's OWN counts ignore the lifecycle selection (siblings don't collapse)…
    assert counts["lifecycle"] == {"active": 1, "eol": 1}
    # …but OTHER facets are narrowed by it.
    assert counts["condition"] == {"New": 1}


def test_global_counts_has_datasheet_self_excludes(db_session: Session):
    _card(db_session, "d1", datasheet_url="https://example.com/ds.pdf", lifecycle_status="active")
    _card(db_session, "d2", lifecycle_status="eol")
    db_session.commit()

    counts = get_global_facet_counts(db_session, filters={"has_datasheet": True})
    # has_datasheet's own count is computed WITHOUT the has_datasheet flag (it would be
    # a tautology), while lifecycle IS narrowed to datasheet-bearing cards.
    assert counts["has_datasheet"] == {"true": 1}
    assert counts["lifecycle"] == {"active": 1}


def test_global_counts_narrow_under_spec_sub_filters(db_session: Session):
    _card(db_session, "d1", interface="SATA", lifecycle_status="active")
    _card(db_session, "d2", interface="SAS", lifecycle_status="eol")
    db_session.commit()

    counts = get_global_facet_counts(db_session, commodity="hdd", filters={"sub_filters": {"interface": ["SATA"]}})
    assert counts["lifecycle"] == {"active": 1}


def test_global_counts_still_exclude_deleted(db_session: Session):
    _card(db_session, "d1", lifecycle_status="active")
    _card(db_session, "d2", lifecycle_status="active", deleted_at=datetime.now(UTC))
    db_session.commit()

    counts = get_global_facet_counts(db_session, filters={"q": "d"})
    assert counts["lifecycle"] == {"active": 1}


# ── Routes: full filter set reaches both count paths ────────────────────


def _seed_hdd_interface_schema(db: Session) -> None:
    db.add(
        CommoditySpecSchema(
            commodity="hdd",
            spec_key="interface",
            display_name="Interface",
            data_type="enum",
            enum_values=["SATA", "SAS", "SCSI"],
            sort_order=1,
            is_filterable=True,
            is_primary=True,
        )
    )


def test_subfilters_route_counts_narrow_under_q(client, db_session: Session):
    _seed_hdd_interface_schema(db_session)
    _card(db_session, "st4000", interface="SATA", description="ENTERPRISE DRIVE")
    _card(db_session, "wd2000", interface="SATA", description="MOBILE DRIVE")
    db_session.commit()

    unfiltered = client.get("/v2/partials/materials/filters/sub?commodity=hdd")
    narrowed = client.get("/v2/partials/materials/filters/sub?commodity=hdd&q=st4000")
    assert unfiltered.status_code == narrowed.status_code == 200
    assert ">2</span>" in unfiltered.text  # SATA count over both cards
    assert ">2</span>" not in narrowed.text
    assert ">1</span>" in narrowed.text  # SATA count under q


def test_subfilters_route_counts_narrow_under_lifecycle(client, db_session: Session):
    _seed_hdd_interface_schema(db_session)
    _card(db_session, "d1", interface="SATA", lifecycle_status="active")
    _card(db_session, "d2", interface="SATA", lifecycle_status="eol")
    db_session.commit()

    resp = client.get("/v2/partials/materials/filters/sub?commodity=hdd&lifecycle=active")
    assert resp.status_code == 200
    assert ">1</span>" in resp.text
    assert ">2</span>" not in resp.text


def test_global_route_counts_narrow_under_q(client, db_session: Session):
    _card(db_session, "d1", lifecycle_status="active", description="ENTERPRISE DRIVE")
    _card(db_session, "d2", lifecycle_status="active", description="MOBILE DRIVE")
    db_session.commit()

    resp = client.get("/v2/partials/materials/filters/global?q=d1")
    assert resp.status_code == 200
    # Lifecycle "Active" renders count 1 (only d1 matches q), not 2.
    assert ">1</span>" in resp.text
    assert ">2</span>" not in resp.text


def test_global_route_self_exclusion_via_params(client, db_session: Session):
    _card(db_session, "d1", lifecycle_status="active")
    _card(db_session, "d2", lifecycle_status="eol")
    db_session.commit()

    resp = client.get("/v2/partials/materials/filters/global?lifecycle=active")
    assert resp.status_code == 200
    # Both lifecycle values still render (self-exclusion): Active 1 + EOL 1.
    assert "Active" in resp.text and "EOL" in resp.text


# ── displays.resolution vocabulary ───────────────────────────────────────


_PANEL_RESOLUTIONS = ["1920x1080", "1366x768", "3840x2160", "1920x1200", "2560x1440", "1280x1024"]


def test_displays_resolution_seed_is_panel_vocabulary():
    seed = next(sp for sp in COMMODITY_SPEC_SEEDS["displays"] if sp["spec_key"] == "resolution")
    assert seed["enum_values"] == _PANEL_RESOLUTIONS
    # The character-LCD formats are gone — they matched nothing in the live data.
    assert not {"16x2", "20x4", "128x64", "128x32"} & set(seed["enum_values"])


def test_reseed_reconciles_displays_resolution_row(db_session: Session):
    # Simulate the live pre-PR row (character-LCD vocabulary) and reconcile.
    seed = next(sp for sp in COMMODITY_SPEC_SEEDS["displays"] if sp["spec_key"] == "resolution")
    db_session.add(
        CommoditySpecSchema(
            commodity="displays",
            spec_key="resolution",
            display_name=seed["display_name"],
            data_type="enum",
            enum_values=["16x2", "20x4", "128x64", "1920x1080"],
            sort_order=seed.get("sort_order", 0),
            is_filterable=True,
            is_primary=False,
        )
    )
    db_session.commit()

    assert reseed_changed_schemas(db_session) >= 1
    row = db_session.query(CommoditySpecSchema).filter_by(commodity="displays", spec_key="resolution").one()
    assert list(row.enum_values) == _PANEL_RESOLUTIONS

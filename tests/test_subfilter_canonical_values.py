"""Unit 2 — get_subfilter_options renders the full canonical vocabulary.

Fixed-vocab enums expose every declared value (so unstocked options show with a (0)
count); open-vocab enums fall back to top-N observed values + a typeahead widget;
booleans always expose Yes/No.
"""

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema, MaterialCard, MaterialSpecFacet
from app.services.faceted_search_service import TOP_N, get_subfilter_options
from tests.conftest import engine  # noqa: F401


def _schema(db: Session, commodity: str, spec_key: str, data_type: str, **kw) -> None:
    db.add(
        CommoditySpecSchema(
            commodity=commodity,
            spec_key=spec_key,
            display_name=kw.get("display_name", spec_key),
            data_type=data_type,
            enum_values=kw.get("enum_values"),
            sort_order=kw.get("sort_order", 0),
            is_filterable=True,
            is_primary=kw.get("is_primary", False),
        )
    )
    db.flush()


def _facet(db: Session, commodity: str, spec_key: str, value_text: str) -> None:
    card = MaterialCard(
        normalized_mpn=f"{spec_key}-{value_text}".lower(),
        display_mpn=f"{spec_key}-{value_text}",
        category=commodity,
    )
    db.add(card)
    db.flush()
    db.add(MaterialSpecFacet(material_card_id=card.id, category=commodity, spec_key=spec_key, value_text=value_text))
    db.flush()


def _opt(db: Session, commodity: str, spec_key: str) -> dict:
    return next(o for o in get_subfilter_options(db, commodity) if o["spec_key"] == spec_key)


def test_fixed_vocab_returns_full_canonical_list(db_session: Session):
    _schema(db_session, "hdd", "interface", "enum", enum_values=["SATA", "SAS", "SCSI"])
    _facet(db_session, "hdd", "interface", "SATA")  # only SATA has data
    opt = _opt(db_session, "hdd", "interface")
    assert opt["values"] == ["SATA", "SAS", "SCSI"]  # SCSI present despite no rows
    assert opt["widget"] == "checkbox"


def test_fixed_vocab_appends_unexpected_observed(db_session: Session):
    _schema(db_session, "hdd", "interface", "enum", enum_values=["SATA", "SAS"])
    _facet(db_session, "hdd", "interface", "IDE")  # observed but not in canonical
    opt = _opt(db_session, "hdd", "interface")
    assert opt["values"] == ["SATA", "SAS", "IDE"]  # canonical first, then unexpected


def test_open_vocab_uses_typeahead_widget(db_session: Session):
    _schema(db_session, "connectors", "series", "enum", enum_values=None)
    for s in ["Micro-Fit", "Mega-Fit", "Mini-Fit"]:
        _facet(db_session, "connectors", "series", s)
    opt = _opt(db_session, "connectors", "series")
    assert opt["widget"] == "typeahead"
    assert opt["total_distinct"] == 3
    assert set(opt["values"]) == {"Micro-Fit", "Mega-Fit", "Mini-Fit"}


def test_open_vocab_caps_at_top_n(db_session: Session):
    _schema(db_session, "connectors", "series", "enum", enum_values=None)
    for i in range(TOP_N + 8):
        _facet(db_session, "connectors", "series", f"series-{i:02d}")
    opt = _opt(db_session, "connectors", "series")
    assert len(opt["values"]) == TOP_N
    assert opt["total_distinct"] == TOP_N + 8


def test_boolean_always_returns_true_false(db_session: Session):
    _schema(db_session, "dram", "ecc", "boolean")  # no facet rows at all
    opt = _opt(db_session, "dram", "ecc")
    assert opt["values"] == ["true", "false"]


def test_numeric_has_range_widget(db_session: Session):
    _schema(db_session, "hdd", "capacity_gb", "numeric")
    opt = _opt(db_session, "hdd", "capacity_gb")
    assert opt["widget"] == "range"

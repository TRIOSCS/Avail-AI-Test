"""tests/test_material_card_specs.py — Per-commodity spec chips on the material list.

Exercises the real /v2/partials/materials/faceted endpoint: commodity-scoped chips
mirror the filter schema (all populated filterable fields, primary first, human
units, +N overflow), scoped spec-less cards get the muted placeholder, and the
unscoped view keeps the primary-keys-per-category behavior with formatted values.

Depends on: conftest.py client/db_session fixtures, CommoditySpecSchema,
MaterialCard.specs_structured.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.faceted_search import CommoditySpecSchema
from app.models.intelligence import MaterialCard


def _seed_cap_schema(db):
    """Capacitor schema: 2 primary + 6 secondary filterable fields (8 total)."""
    rows = [
        ("capacitance", "Capacitance", "numeric", "pF", 1, True),
        ("voltage_rating", "Voltage Rating (V)", "numeric", "V", 2, True),
        ("dielectric", "Dielectric", "enum", None, 3, False),
        ("tolerance", "Tolerance", "enum", None, 4, False),
        ("package", "Package", "enum", None, 5, False),
        ("mounting", "Mounting", "enum", None, 6, False),
        ("rated_temp", "Rated Temp (C)", "numeric", "C", 7, False),
        ("halogen_free", "Halogen Free", "boolean", None, 8, False),
    ]
    for key, name, dtype, unit, order, primary in rows:
        db.add(
            CommoditySpecSchema(
                commodity="capacitors",
                spec_key=key,
                display_name=name,
                data_type=dtype,
                unit=unit,
                canonical_unit=unit,
                sort_order=order,
                is_filterable=True,
                is_primary=primary,
            )
        )
    db.flush()


def _cap_card(db, mpn="GRM188R71H104", specs=None):
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        manufacturer="Murata",
        category="capacitors",
        specs_structured=specs,
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.flush()
    return card


_FULL_SPECS = {
    "capacitance": {"value": 100000, "source": "test"},  # 100000 pF -> "100 nF"
    "voltage_rating": {"value": 50, "source": "test"},
    "dielectric": {"value": "X7R", "source": "test"},
    "tolerance": {"value": "±10%", "source": "test"},
    "package": {"value": "0603", "source": "test"},
    "mounting": {"value": "SMD", "source": "test"},
    "rated_temp": {"value": 125, "source": "test"},
    "halogen_free": {"value": True, "source": "test"},
}


def test_scoped_chips_mirror_filter_fields_with_units(client, db_session: Session):
    _seed_cap_schema(db_session)
    _cap_card(db_session, specs=_FULL_SPECS)
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?commodity=capacitors")
    assert resp.status_code == 200
    # Labels match the filter sidebar's display names; values carry human units.
    assert "Capacitance" in resp.text
    assert "100 nF" in resp.text  # canonical 100000 pF, SI-promoted
    assert "100000" not in resp.text  # never the raw magnitude
    assert "50 V" in resp.text
    # Primary fields lead, then sidebar order; 8 populated - 6 cap = "+2 more".
    assert "+2 more" in resp.text
    # The two dropped fields are the sort-order tail (rated_temp, halogen_free).
    assert "Rated Temp" not in resp.text
    assert "X7R" in resp.text


def test_scoped_speccless_card_gets_placeholder(client, db_session: Session):
    _seed_cap_schema(db_session)
    _cap_card(db_session, specs=None)
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted?commodity=capacitors")
    assert resp.status_code == 200
    assert "No specs recorded" in resp.text


def test_unscoped_shows_primary_fields_formatted(client, db_session: Session):
    _seed_cap_schema(db_session)
    _cap_card(db_session, specs=_FULL_SPECS)
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    # Unscoped = the card's own category's PRIMARY fields only, still formatted.
    assert "100 nF" in resp.text
    assert "50 V" in resp.text
    # Secondary fields don't render unscoped.
    assert "X7R" not in resp.text
    # No placeholder in the unscoped view.
    assert "No specs recorded" not in resp.text


def test_unscoped_speccless_card_shows_no_placeholder(client, db_session: Session):
    _cap_card(db_session, specs=None)
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    assert "No specs recorded" not in resp.text


def test_off_vocab_category_falls_back_to_raw_spec_keys(client, db_session: Session):
    # ~35 legacy prod cards carry non-canonical categories; the ORM guard is
    # write-time only, so the list must render them via the prettified-key fallback.
    card = MaterialCard(
        normalized_mpn="legacy-1",
        display_mpn="LEGACY-1",
        manufacturer="OldCo",
        specs_structured={"weird_key": {"value": 42, "source": "test"}},
        created_at=datetime.now(UTC),
    )
    # Bypass the @validates guard the way legacy rows predate it.
    db_session.add(card)
    db_session.flush()
    db_session.execute(MaterialCard.__table__.update().where(MaterialCard.id == card.id).values(category="EEPROM"))
    db_session.commit()

    resp = client.get("/v2/partials/materials/faceted")
    assert resp.status_code == 200
    assert "weird key" in resp.text
    assert "42" in resp.text

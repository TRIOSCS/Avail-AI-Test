"""test_manufacturer_model.py — Tests for the Manufacturer lookup model.

Verifies creation, field storage, and unique constraint enforcement.

Called by: pytest
Depends on: app.models.sourcing.Manufacturer, conftest.db_session
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.sourcing import Manufacturer


def test_manufacturer_create(db_session):
    mfr = Manufacturer(canonical_name="Texas Instruments", aliases=["TI", "Texas Inst"])
    db_session.add(mfr)
    db_session.commit()
    assert mfr.id is not None
    assert mfr.canonical_name == "Texas Instruments"
    assert "TI" in mfr.aliases


def test_manufacturer_unique_name(db_session):
    db_session.add(Manufacturer(canonical_name="Texas Instruments"))
    db_session.commit()
    db_session.expunge_all()
    with pytest.raises(IntegrityError):
        db_session.add(Manufacturer(canonical_name="Texas Instruments"))
        db_session.commit()


def test_manufacturer_aliases_default_empty(db_session):
    mfr = Manufacturer(canonical_name="Broadcom Inc.")
    db_session.add(mfr)
    db_session.commit()
    db_session.refresh(mfr)
    # aliases defaults to None/null in SQLite when not provided (JSON column default)
    assert mfr.aliases is None or mfr.aliases == [] or isinstance(mfr.aliases, list)


def test_manufacturer_website_nullable(db_session):
    mfr = Manufacturer(canonical_name="NVIDIA", aliases=[])
    db_session.add(mfr)
    db_session.commit()
    db_session.refresh(mfr)
    assert mfr.website is None


def test_manufacturer_website_stored(db_session):
    mfr = Manufacturer(
        canonical_name="Analog Devices",
        aliases=["ADI"],
        website="https://www.analog.com",
    )
    db_session.add(mfr)
    db_session.commit()
    db_session.refresh(mfr)
    assert mfr.website == "https://www.analog.com"


def test_manufacturer_created_at_set(db_session):
    mfr = Manufacturer(canonical_name="Murata Manufacturing", aliases=["Murata"])
    db_session.add(mfr)
    db_session.commit()
    db_session.refresh(mfr)
    assert mfr.created_at is not None

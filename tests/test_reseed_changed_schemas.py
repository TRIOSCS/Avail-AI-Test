"""Unit 3 — reseed_changed_schemas reconciles drifted commodity_spec_schemas rows.

The boot seeder never updates an existing row, so curated seed changes (new enum values,
promoted primary facet, reordered specs) need delete-then-reinsert. reseed only touches
CHANGED existing rows; brand-new pairs remain the boot seeder's job.
"""

from sqlalchemy.orm import Session

from app.models import CommoditySpecSchema
from app.services.commodity_registry import (
    COMMODITY_SPEC_SEEDS,
    reseed_changed_schemas,
    seed_commodity_schemas,
)
from tests.conftest import engine  # noqa: F401


def _seed_lookup(commodity: str, spec_key: str) -> dict:
    return next(sp for sp in COMMODITY_SPEC_SEEDS[commodity] if sp["spec_key"] == spec_key)


def test_reseed_reconciles_changed_enum_values(db_session: Session):
    seed = _seed_lookup("hdd", "interface")
    db_session.add(
        CommoditySpecSchema(
            commodity="hdd",
            spec_key="interface",
            display_name=seed["display_name"],
            data_type="enum",
            enum_values=["SATA", "SAS", "NVMe"],  # stale (pre-rework)
            sort_order=seed.get("sort_order", 0),
            is_filterable=True,
            is_primary=False,
        )
    )
    db_session.commit()

    n = reseed_changed_schemas(db_session)
    assert n >= 1

    row = db_session.query(CommoditySpecSchema).filter_by(commodity="hdd", spec_key="interface").one()
    assert set(row.enum_values) == set(seed["enum_values"])
    assert "SCSI" in row.enum_values  # the curated addition is now present


def test_reseed_returns_zero_when_in_sync(db_session: Session):
    seed_commodity_schemas(db_session)  # insert every canonical row exactly
    before_id = db_session.query(CommoditySpecSchema).filter_by(commodity="hdd", spec_key="interface").one().id

    assert reseed_changed_schemas(db_session) == 0

    after_id = db_session.query(CommoditySpecSchema).filter_by(commodity="hdd", spec_key="interface").one().id
    assert before_id == after_id  # unchanged row was not delete-then-reinserted


def test_reseed_is_idempotent(db_session: Session):
    db_session.add(
        CommoditySpecSchema(
            commodity="hdd",
            spec_key="interface",
            display_name="Interface",
            data_type="enum",
            enum_values=["SATA"],
            sort_order=2,
            is_filterable=True,
            is_primary=False,
        )
    )
    db_session.commit()

    assert reseed_changed_schemas(db_session) >= 1
    assert reseed_changed_schemas(db_session) == 0  # second pass is a no-op


def test_reseed_ignores_missing_pairs_seeder_inserts_them(db_session: Session):
    # reseed only reconciles EXISTING rows — it never inserts a brand-new pair.
    assert reseed_changed_schemas(db_session) == 0
    assert db_session.query(CommoditySpecSchema).count() == 0

    # The boot seeder is what inserts new pairs (e.g. the new hdd.usage_class facet).
    assert seed_commodity_schemas(db_session) > 0
    assert db_session.query(CommoditySpecSchema).filter_by(commodity="hdd", spec_key="usage_class").count() == 1


def test_reseed_detects_sort_order_change(db_session: Session):
    seed = _seed_lookup("hdd", "capacity_gb")
    db_session.add(
        CommoditySpecSchema(
            commodity="hdd",
            spec_key="capacity_gb",
            display_name=seed["display_name"],
            data_type="numeric",
            unit=seed.get("unit"),
            canonical_unit=seed.get("canonical_unit"),
            sort_order=99,  # drifted
            is_filterable=True,
            is_primary=True,
        )
    )
    db_session.commit()

    assert reseed_changed_schemas(db_session) >= 1
    row = db_session.query(CommoditySpecSchema).filter_by(commodity="hdd", spec_key="capacity_gb").one()
    assert row.sort_order == seed.get("sort_order", 0)

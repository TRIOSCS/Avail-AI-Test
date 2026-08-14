"""QC regression: SF-import offers must stay visible to the proactive scan.

The Salesforce proactive-export importer (app.management.import_proactive_export)
writes each availability as an Offer BACKDATED to its SF created_date. The proactive
batch scan only ever considers ``Offer.created_at > watermark`` (a SystemConfig key
persisted in proactive_matching). If the watermark already sits ahead of a freshly
imported backdated offer, that offer is invisible to every future scan and never seeds
a ProactiveMatch — the bug this test pins.

The fix rewinds that shared watermark to just behind the oldest imported offer whenever
an --apply actually inserts offers. These tests assert the watermark lands at/behind the
offer's created_at (so ``created_at > watermark`` is true for it), that the guard only
moves the watermark BACKWARD, and that a no-op (idempotent) re-import leaves it alone.

Called by: pytest.
Depends on: app.management.import_proactive_export.import_proactive_export,
    app.services.proactive_matching (_get_watermark/_set_watermark/_WATERMARK_KEY),
    app.models (Offer, User), app.models.config.SystemConfig.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.management.import_proactive_export import import_proactive_export
from app.models import Offer, User
from app.models.config import SystemConfig
from app.services.proactive_matching import _get_watermark, _set_watermark, run_proactive_scan

# A single backdated availability row (SF created_date 8/5/2026 → midnight UTC).
_AVAIL_HEADER = "material_name,sourcing_item_number,vendor_account_name,qty,outright_price,owner_full_name,created_date"
_AVAIL_ROW = "QCM-PART-001,SRC#-QCM-0001,Acme Distributors,25,$12.50,Some Rep,8/5/2026"
_BACKDATED_CREATED_AT = datetime(2026, 8, 5, tzinfo=UTC)


def _write_csvs(tmp_path: Path) -> tuple[Path, Path]:
    """Write a minimal requirements+availabilities pair; only the offer row matters."""
    req = tmp_path / "requirements.csv"
    req.write_text(
        "material_name,req_item_number,customer_account_name,requisition_owner_full_name,"
        "qty,target_price_per_unit,sourcing_status,created_date\n",
        encoding="utf-8",
    )
    avail = tmp_path / "availabilities.csv"
    avail.write_text(f"{_AVAIL_HEADER}\n{_AVAIL_ROW}\n", encoding="utf-8")
    return req, avail


def _actor(db) -> User:
    actor = User(email="qcm-admin@example.com", name="QCM Admin", role="admin", azure_id="qcm-adm-1")
    db.add(actor)
    db.flush()
    return actor


def test_apply_rewinds_watermark_behind_backdated_offer(db_session, tmp_path):
    """A watermark already AHEAD of the imported offer is rewound behind its
    created_at."""
    # Watermark parked in the future — the exact condition that hides the backdated import.
    ahead = datetime.now(UTC) + timedelta(days=1)
    _set_watermark(db_session, ahead)

    req, avail = _write_csvs(tmp_path)
    stats = import_proactive_export(
        db_session, requirements_csv=req, availabilities_csv=avail, actor=_actor(db_session)
    )
    db_session.commit()

    assert stats["offers_created"] == 1
    offer = db_session.query(Offer).filter(Offer.notes == "SF sourcing item SRC#-QCM-0001").one()
    assert offer.created_at.replace(tzinfo=UTC) == _BACKDATED_CREATED_AT

    watermark = _get_watermark(db_session)
    # Watermark now sits at/behind the offer's created_at → the scan's strict
    # ``created_at > watermark`` filter includes it.
    assert watermark <= _BACKDATED_CREATED_AT
    assert watermark < offer.created_at.replace(tzinfo=UTC)


def test_rewound_watermark_makes_offer_scannable(db_session, tmp_path):
    """End-to-end: after import, the batch scan actually picks up the backdated offer."""
    _set_watermark(db_session, datetime.now(UTC) + timedelta(days=1))
    req, avail = _write_csvs(tmp_path)
    import_proactive_export(db_session, requirements_csv=req, availabilities_csv=avail, actor=_actor(db_session))
    db_session.commit()

    result = run_proactive_scan(db_session)
    # The offer is inside the scan window again (0 matches is fine — no demand seeded;
    # the point is it was SCANNED rather than filtered out by a stale watermark).
    assert result["scanned_offers"] >= 1


def test_apply_does_not_move_watermark_forward(db_session, tmp_path):
    """Guard is backward-only: a watermark already OLDER than the offer is left untouched."""
    old = datetime(2026, 1, 1, tzinfo=UTC)
    _set_watermark(db_session, old)
    req, avail = _write_csvs(tmp_path)
    import_proactive_export(db_session, requirements_csv=req, availabilities_csv=avail, actor=_actor(db_session))
    db_session.commit()

    # Unchanged — never dragged forward toward the newer offer created_at.
    assert _get_watermark(db_session) == old


def test_noop_reimport_leaves_watermark_untouched(db_session, tmp_path):
    """Idempotent re-run inserts no offers, so it must not rewind an unrelated
    watermark."""
    req, avail = _write_csvs(tmp_path)
    actor = _actor(db_session)
    import_proactive_export(db_session, requirements_csv=req, availabilities_csv=avail, actor=actor)
    db_session.commit()

    parked = datetime.now(UTC) + timedelta(days=5)
    _set_watermark(db_session, parked)
    db_session.commit()

    again = import_proactive_export(db_session, requirements_csv=req, availabilities_csv=avail, actor=actor)
    db_session.commit()

    assert again["offers_created"] == 0
    assert again["rows_skipped_existing"] == 1
    # No new offers → the parked watermark is preserved, not rewound.
    assert _get_watermark(db_session) == parked
    # Only the single watermark SystemConfig row exists (no duplicate key written).
    rows = db_session.query(SystemConfig).filter(SystemConfig.key == "proactive_last_scan").all()
    assert len(rows) == 1

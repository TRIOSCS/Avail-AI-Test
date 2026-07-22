"""tests/test_reattribute_activity.py — app/management/reattribute_activity.py (ISS-030
backfill).

Covers: orphaned ActivityLog rows (requisition_id set, company_id/vendor_card_id both
NULL) re-attributed via match_email_to_entity; own-domain/junk counterparty rows flagged
is_meaningful=False instead; dry-run-by-default parity (no writes until --apply); rows
that already have an attribution or lack a contact_email are left untouched; idempotency
on a second apply pass.

Called by: pytest
Depends on: app/management/reattribute_activity.py, app.models.ActivityLog,
    conftest.py (db_session, test_company, test_requisition).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.management.reattribute_activity import main, run_backfill
from app.models import ActivityLog, Company, Requisition


def _orphan_row(db: Session, req: Requisition, email: str, **kw) -> ActivityLog:
    row = ActivityLog(
        requisition_id=req.id,
        activity_type="email_received",
        channel="email",
        contact_email=email,
        summary="orphaned row",
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(row)
    db.flush()
    return row


def _requisition(db: Session) -> Requisition:
    req = Requisition(name="Backfill Req", status="open")
    db.add(req)
    db.flush()
    return req


def test_dry_run_writes_nothing(db_session: Session):
    """Dry-run (default) computes the tally but never mutates the row."""
    req = _requisition(db_session)
    company = Company(name="Reattrib Co", domain="reattrib-co.com", is_active=True)
    db_session.add(company)
    db_session.flush()
    row = _orphan_row(db_session, req, "buyer@reattrib-co.com")
    db_session.commit()

    stats = run_backfill(db_session, apply=False)

    db_session.refresh(row)
    assert row.company_id is None, "dry-run must not write company_id"
    assert stats["company_attributed"] == 1
    assert stats["scanned"] == 1


def test_apply_resolves_company_attribution(db_session: Session):
    """--apply fills company_id when the counterparty domain matches a Company."""
    req = _requisition(db_session)
    company = Company(name="Applied Co", domain="applied-co.com", is_active=True)
    db_session.add(company)
    db_session.flush()
    row = _orphan_row(db_session, req, "buyer@applied-co.com")
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    db_session.refresh(row)
    assert row.company_id == company.id
    assert row.vendor_card_id is None
    assert stats["company_attributed"] == 1


def test_apply_flags_own_domain_row_not_meaningful(db_session: Session):
    """A row whose counterparty is the org's own domain (and doesn't independently
    resolve) is flagged is_meaningful=False, not attributed — and stamped quality-
    assessed (quality_assessed_at + quality_classification="internal") like the write
    path, so score_unscored_activities (quality_assessed_at IS NULL) can never AI re-
    promote the demotion."""
    req = _requisition(db_session)
    row = _orphan_row(db_session, req, "colleague@trioscs.com")
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    db_session.refresh(row)
    assert row.company_id is None
    assert row.vendor_card_id is None
    assert row.is_meaningful is False
    assert row.quality_classification == "internal", "must carry the write path's demotion classification"
    assert row.quality_assessed_at is not None, "must be stamped assessed so the AI pass never rescores it"
    assert stats["flagged_noise"] == 1


def test_apply_flags_junk_domain_row_with_same_stamps(db_session: Session):
    """A junk-domain counterparty row gets the identical demotion stamps as the own-
    domain case — one shared helper, one definition of a demoted row."""
    req = _requisition(db_session)
    row = _orphan_row(db_session, req, "noreply@gmail.com")
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    db_session.refresh(row)
    assert row.is_meaningful is False
    assert row.quality_classification == "internal"
    assert row.quality_assessed_at is not None
    assert stats["flagged_noise"] == 1


def test_row_without_contact_email_left_untouched(db_session: Session):
    """A row with no contact_email has nothing to resolve — the candidate query excludes
    it entirely (left alone, no deletion, no mutation)."""
    req = _requisition(db_session)
    row = _orphan_row(db_session, req, None)
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    db_session.refresh(row)
    assert row.company_id is None
    assert row.is_meaningful is None
    assert stats["scanned"] == 0


def test_already_attributed_row_excluded_from_scan(db_session: Session):
    """A row that already has company_id set is out of scope — the candidate query
    excludes it, and running the backfill is a no-op for it."""
    req = _requisition(db_session)
    company = Company(name="Already Co", domain="already-co.com", is_active=True)
    db_session.add(company)
    db_session.flush()
    row = _orphan_row(db_session, req, "buyer@already-co.com", company_id=company.id)
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    assert stats["scanned"] == 0


def test_idempotent_second_apply_pass_is_stable(db_session: Session):
    """Running --apply twice ends in the same state (idempotent)."""
    req = _requisition(db_session)
    company = Company(name="Idempotent Co", domain="idempotent-co.com", is_active=True)
    db_session.add(company)
    db_session.flush()
    row = _orphan_row(db_session, req, "buyer@idempotent-co.com")
    db_session.commit()

    run_backfill(db_session, apply=True)
    db_session.refresh(row)
    assert row.company_id == company.id

    stats_second = run_backfill(db_session, apply=True)
    db_session.refresh(row)
    assert row.company_id == company.id
    assert stats_second["scanned"] == 0, "already-attributed row must fall out of scope"


def test_limit_caps_scanned_rows(db_session: Session):
    """--limit bounds how many candidate rows a pass touches."""
    req = _requisition(db_session)
    for i in range(3):
        _orphan_row(db_session, req, f"buyer{i}@limited-co.com")
    db_session.commit()

    stats = run_backfill(db_session, apply=False, limit=2)
    assert stats["scanned"] == 2


def test_cli_main_dry_run_exits_zero(db_session: Session, monkeypatch):
    """The CLI entry point runs dry-run by default and exits 0."""
    import app.database as dbmod

    class _SessionProxy:
        """Hand main() the test session but swallow its close() (conftest owns it)."""

        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            return getattr(db_session, name)

    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _SessionProxy())

    exit_code = main([])
    assert exit_code == 0

"""tests/test_demote_own_domain_activity.py —
app/management/demote_own_domain_activity.py (ISS-030 follow-up backfill).

Covers: attributed ActivityLog rows (company_id set) whose contact_email is on the org's
own domain (settings.own_domains) demoted to is_meaningful=False; external-domain and
null-email rows left untouched; dry-run-by-default parity (no writes until --apply);
candidate scope = the Activity tab's visibility predicate (is_meaningful TRUE OR NULL —
unscored own-domain rows surface too, so they must be demoted); rows already
is_meaningful=False are out of scope; keyset-batched scan covers rows beyond one chunk;
idempotency on a second apply pass; --limit cap; CLI entry point.

Called by: pytest
Depends on: app/management/demote_own_domain_activity.py, app.models.ActivityLog,
    conftest.py (db_session).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.management.demote_own_domain_activity import main, run_backfill
from app.models import ActivityLog, Company


def _company(db: Session, name: str, domain: str) -> Company:
    company = Company(name=name, domain=domain, is_active=True)
    db.add(company)
    db.flush()
    return company


def _attributed_row(db: Session, company: Company, email: str | None, **kw) -> ActivityLog:
    kw.setdefault("is_meaningful", True)
    row = ActivityLog(
        company_id=company.id,
        activity_type="email_received",
        channel="email",
        contact_email=email,
        summary="attributed row",
        created_at=datetime.now(UTC),
        **kw,
    )
    db.add(row)
    db.flush()
    return row


def test_dry_run_counts_but_writes_nothing(db_session: Session):
    """Dry-run (default) tallies the own-domain row but never mutates it."""
    own_co = _company(db_session, "Trio Own Co", "trioscs.com")
    ext_co = _company(db_session, "External Co", "external-co.com")
    own_row = _attributed_row(db_session, own_co, "colleague@trioscs.com")
    ext_row = _attributed_row(db_session, ext_co, "buyer@external-co.com")
    db_session.commit()

    stats = run_backfill(db_session, apply=False)

    db_session.refresh(own_row)
    db_session.refresh(ext_row)
    assert own_row.is_meaningful is True, "dry-run must not flip is_meaningful"
    assert ext_row.is_meaningful is True
    assert stats["scanned"] == 2
    assert stats["own_domain_flagged"] == 1
    assert stats["external_skipped"] == 1


def test_apply_flips_only_own_domain_rows(db_session: Session):
    """--apply sets is_meaningful=False on own-domain attributed rows only."""
    own_co = _company(db_session, "Trio Apply Co", "trioscs.com")
    ext_co = _company(db_session, "External Apply Co", "ext-apply-co.com")
    own_row = _attributed_row(db_session, own_co, "colleague@trioscs.com")
    ext_row = _attributed_row(db_session, ext_co, "buyer@ext-apply-co.com")
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    db_session.refresh(own_row)
    db_session.refresh(ext_row)
    assert own_row.is_meaningful is False
    assert ext_row.is_meaningful is True, "external-domain row must be untouched"
    assert stats["own_domain_flagged"] == 1
    assert stats["external_skipped"] == 1


def test_null_email_row_excluded_from_scan(db_session: Session):
    """A row with no contact_email has no domain to test — the candidate query excludes
    it entirely (no mutation)."""
    own_co = _company(db_session, "Trio Null Co", "trioscs.com")
    row = _attributed_row(db_session, own_co, None)
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    db_session.refresh(row)
    assert row.is_meaningful is True
    assert stats["scanned"] == 0


def test_unattributed_row_out_of_scope(db_session: Session):
    """A row with company_id NULL is reattribute_activity's territory — the candidate
    query here requires company_id NOT NULL."""
    row = ActivityLog(
        company_id=None,
        activity_type="email_received",
        channel="email",
        contact_email="colleague@trioscs.com",
        is_meaningful=True,
        summary="orphaned row",
        created_at=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    db_session.refresh(row)
    assert row.is_meaningful is True
    assert stats["scanned"] == 0


def test_already_demoted_row_excluded_from_scan(db_session: Session):
    """Rows already is_meaningful=False fall out of the candidate query."""
    own_co = _company(db_session, "Trio Done Co", "trioscs.com")
    _attributed_row(db_session, own_co, "colleague@trioscs.com", is_meaningful=False)
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    assert stats["scanned"] == 0


def test_null_scored_own_domain_row_demoted(db_session: Session):
    """H2: the Activity tab shows is_meaningful TRUE OR NULL (_is_meaningful_or_unscored),
    so an unscored (NULL) own-domain attributed row still surfaces — the candidate query
    must include NULL rows and --apply must set them False. External NULL rows are left
    for the AI quality pass."""
    own_co = _company(db_session, "Trio NullScore Co", "trioscs.com")
    ext_co = _company(db_session, "External NullScore Co", "ext-nullscore.com")
    own_null = _attributed_row(db_session, own_co, "colleague@trioscs.com", is_meaningful=None)
    ext_null = _attributed_row(db_session, ext_co, "buyer@ext-nullscore.com", is_meaningful=None)
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    db_session.refresh(own_null)
    db_session.refresh(ext_null)
    assert own_null.is_meaningful is False, "unscored own-domain row surfaces on the tab — must be demoted"
    assert ext_null.is_meaningful is None, "external NULL row stays unscored for the AI pass"
    assert stats["scanned"] == 2
    assert stats["own_domain_flagged"] == 1
    assert stats["external_skipped"] == 1


async def test_apply_stamps_assessed_and_ai_pass_skips_row(db_session: Session, monkeypatch):
    """--apply must stamp quality_assessed_at + quality_classification="internal" exactly
    like the write path — an unstamped demoted row <7 days old with an AI-scored
    activity_type (email_received) would be selected by score_unscored_activities
    (quality_assessed_at IS NULL) and AI re-promoted, undoing the demotion."""
    import app.services.activity_quality_service as aqs

    own_co = _company(db_session, "Trio Stamp Co", "trioscs.com")
    # Fresh row: created now (< 7 days), email_received is in _AI_SCORED_TYPES.
    row = _attributed_row(db_session, own_co, "colleague@trioscs.com")
    db_session.commit()

    run_backfill(db_session, apply=True)

    db_session.refresh(row)
    assert row.is_meaningful is False
    assert row.quality_classification == "internal", "must carry the write path's demotion classification"
    assert row.quality_assessed_at is not None, "must be stamped assessed so the AI pass never rescores it"

    # Prove the AI pass no longer selects it: score_unscored_activities filters on
    # quality_assessed_at IS NULL — with the stamp, the stubbed scorer is never called.
    calls: list[int] = []

    async def _spy_scorer(activity_id, db):
        calls.append(activity_id)

    monkeypatch.setattr(aqs, "score_activity", _spy_scorer)
    scored = await aqs.score_unscored_activities(db_session)
    assert scored == 0
    assert calls == [], "stamped row must fall out of score_unscored_activities' candidate query"


def test_scan_chunks_cover_all_rows(db_session: Session, monkeypatch):
    """H3: candidates stream in id-keyset batches (no full-table .all() materialization)
    — a pass with more rows than one batch still scans and demotes every own-domain
    row."""
    import app.management.demote_own_domain_activity as mod

    monkeypatch.setattr(mod, "_SCAN_CHUNK", 2)
    own_co = _company(db_session, "Trio Chunk Co", "trioscs.com")
    rows = [_attributed_row(db_session, own_co, f"colleague{i}@trioscs.com") for i in range(5)]
    db_session.commit()

    stats = run_backfill(db_session, apply=True)

    for row in rows:
        db_session.refresh(row)
        assert row.is_meaningful is False
    assert stats["scanned"] == 5
    assert stats["own_domain_flagged"] == 5


def test_idempotent_second_apply_pass_is_stable(db_session: Session):
    """Running --apply twice ends in the same state (idempotent) — the flipped row falls
    out of the candidate query on the second pass."""
    own_co = _company(db_session, "Trio Idem Co", "trioscs.com")
    row = _attributed_row(db_session, own_co, "colleague@trioscs.com")
    db_session.commit()

    run_backfill(db_session, apply=True)
    db_session.refresh(row)
    assert row.is_meaningful is False

    stats_second = run_backfill(db_session, apply=True)
    db_session.refresh(row)
    assert row.is_meaningful is False
    assert stats_second["scanned"] == 0, "demoted row must fall out of scope"


def test_limit_caps_scanned_rows(db_session: Session):
    """--limit bounds how many candidate rows a pass touches."""
    own_co = _company(db_session, "Trio Limit Co", "trioscs.com")
    for i in range(3):
        _attributed_row(db_session, own_co, f"colleague{i}@trioscs.com")
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

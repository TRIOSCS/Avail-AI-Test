"""Validation: AVAIL's own matching reproduces the user's 2026-08-06 manual run.

The fixture CSVs are the extract of the Salesforce "Requirements (2YR)/
Availabilities (7DAY)" export dated 2026-08-06 11:41 — the same data the user
matched by hand. These three lines must reproduce EXACTLY on the main part
line (spec, Validation section); the rule is never adjusted to fit:

  GSOT36C-E3-08       | last asked 7/30/2026 for 500,000 | available 1,805,521, low $0.05  (Beckhoff)
  LTSR15-NP           | last asked 8/3/2026 for 3,000 (2 requests) | available 4,850, low $6.44 (Beckhoff)
  BSM300GA120DN2HOSA1 | last asked 7/28/2026 for 950 | available 96, low $274 (RES Spain)

Low cost is MIN over POSITIVE prices ($0.00 = price not provided — GSOT36C
carries two zero-priced rows that count toward qty only; decision surfaced to
the user 2026-08-06). The windows are pinned to the export date so the fixture
stays valid as real time passes.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.management.import_proactive_export import import_proactive_export
from app.models import Company, ProactiveMatch, User
from app.services.proactive_matching import compute_offer_rollup, find_matches_for_offer, part_key
from tests.conftest import engine  # noqa: F401

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "proactive_sf_export"
EXPORT_DAY = datetime(2026, 8, 6, 23, 59, tzinfo=UTC)


@pytest.fixture
def seeded(db_session, monkeypatch):
    """Import the fixture CSVs and pin the matching windows to the export date."""
    monkeypatch.setattr(
        "app.services.proactive_matching._offer_window_start",
        lambda: datetime(2026, 7, 30, 0, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "app.services.proactive_matching._requirement_window_start",
        lambda: datetime(2024, 8, 6, 0, 0, tzinfo=UTC),
    )
    actor = User(email="admin@trioscs.com", name="Import Admin", role="admin", azure_id="adm-1")
    martina = User(email="mtewes@trioscs.com", name="Martina Tewes", role="sales", azure_id="mt-1")
    db_session.add_all([actor, martina])
    db_session.flush()
    stats = import_proactive_export(
        db_session,
        requirements_csv=FIXTURE_DIR / "requirements.csv",
        availabilities_csv=FIXTURE_DIR / "availabilities.csv",
        actor=actor,
    )
    db_session.commit()
    return {"actor": actor, "martina": martina, "stats": stats}


def _match_for(db, part: str) -> ProactiveMatch:
    """Run matching from any live offer of the part, return its match line."""
    from app.models import Offer

    offer = db.query(Offer).filter(Offer.mpn == part).first()
    assert offer is not None, f"no imported offer for {part}"
    matches = find_matches_for_offer(offer.id, db)
    db.commit()
    assert len(matches) == 1, f"{part}: expected one line, got {len(matches)}"
    return matches[0]


def test_import_counts(seeded, db_session):
    stats = seeded["stats"]
    assert stats["requirements_created"] == 42
    assert stats["offers_created"] == 170
    # Re-run is a no-op (idempotent by ReqItem# / SRC#).
    again = import_proactive_export(
        db_session,
        requirements_csv=FIXTURE_DIR / "requirements.csv",
        availabilities_csv=FIXTURE_DIR / "availabilities.csv",
        actor=seeded["actor"],
    )
    assert again["requirements_created"] == 0
    assert again["offers_created"] == 0


def test_line_gsot36c(seeded, db_session):
    r = compute_offer_rollup(db_session, part="GSOT36C-E3-08")
    assert r["offer_count"] == 36
    assert r["available_qty"] == 1_805_521
    assert r["low_cost"] == 0.05  # min POSITIVE price; two $0.00 rows count toward qty only

    m = _match_for(db_session, "GSOT36C-E3-08")
    assert m.last_asked_at.date() == datetime(2026, 7, 30, tzinfo=UTC).date()
    assert m.last_asked_qty == 500_000
    assert m.requirement_count == 1
    company = db_session.get(Company, m.company_id)
    assert company.name.startswith("Beckhoff Automation")
    assert m.salesperson_id == seeded["martina"].id  # rep matched by name


def test_line_ltsr15np(seeded, db_session):
    """2026-08-08 rule change (user-approved): formatting variants pool.

    'LTSR 15-NP' is the same part as LTSR15-NP once spaces/case are ignored, so the
    2026-08-06 hand-checked 4,850 becomes 5,850 pooled (4,850 exact + 1,000 under the
    space spelling), flagged as a formatting variant — never a silent merge. Low cost
    stays $6.44 (the variant's $10 doesn't lower it).
    """
    r = compute_offer_rollup(db_session, part="LTSR15-NP")
    assert r["offer_count"] == 4  # 3 exact + 1 variant spelling
    assert r["available_qty"] == 5_850
    assert r["low_cost"] == 6.44
    assert r["variants"]["LTSR 15-NP"] == {"qty": 1_000, "kind": "formatting", "reason": "formatting variant"}
    assert r["has_ai_variants"] is False  # formatting is deterministic, not an AI guess

    m = _match_for(db_session, "LTSR15-NP")
    assert m.last_asked_at.date() == datetime(2026, 8, 3, tzinfo=UTC).date()
    assert m.last_asked_qty == 3_000
    assert m.requirement_count == 3  # 8/3 + 6/30 exact + 3/30 under the variant spelling
    company = db_session.get(Company, m.company_id)
    assert company.name.startswith("Beckhoff Automation")


def test_line_bsm300(seeded, db_session):
    r = compute_offer_rollup(db_session, part="BSM300GA120DN2HOSA1")
    assert r["offer_count"] == 1
    assert r["available_qty"] == 96
    assert r["low_cost"] == 274.0

    m = _match_for(db_session, "BSM300GA120DN2HOSA1")
    assert m.last_asked_at.date() == datetime(2026, 7, 28, tzinfo=UTC).date()
    assert m.last_asked_qty == 950
    company = db_session.get(Company, m.company_id)
    assert company.name.startswith("RES - Renewable Energy Systems")


def test_variant_material_pools_from_either_spelling(seeded, db_session):
    """Both spellings resolve to the SAME pooled class (2026-08-08 rule change); the
    digest's duplicate-materials section still flags the split for cleanup in the system
    of record."""
    variant = part_key("LTSR 15-NP")
    r = compute_offer_rollup(db_session, part=variant)
    assert r["offer_count"] == 4
    assert r["available_qty"] == 5_850
    assert r["low_cost"] == 6.44
    assert "LTSR15-NP" in r["variants"]  # the exact spelling is the variant from this side

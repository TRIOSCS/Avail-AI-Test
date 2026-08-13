"""tests/test_part_equivalence.py — AI part-equivalence: candidates, verdicts, pooling.

Formatting variants pool automatically (same normalized key); AI 'same'
verdicts pool as flagged variants the UI color-codes for double-checking;
'different'/'uncertain'/absent never pool; a human verdict outranks the AI.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session), app.main
"""

import os

os.environ["TESTING"] = "1"

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models import (
    Company,
    CustomerSite,
    Offer,
    PartEquivalence,
    Requirement,
    Requisition,
    User,
)
from app.services.part_equivalence import (
    classify_new_pairs,
    expand_part,
    find_candidate_pairs,
    norm_key,
    record_human_verdict,
)
from app.services.proactive_matching import compute_offer_rollup, find_matches_for_offer
from app.utils.normalization import normalize_mpn_key
from tests.conftest import engine  # noqa: F401

HX = {"HX-Request": "true"}


def _eq(db, a, b, verdict, *, source="ai", reason="pkg suffix"):
    ka, kb = sorted([norm_key(a), norm_key(b)])
    db.add(PartEquivalence(key_a=ka, key_b=kb, example_a=a, example_b=b, verdict=verdict, source=source, reason=reason))
    db.commit()


# ── Candidates ───────────────────────────────────────────────────────────


def test_candidates_suffix_and_near_miss():
    pairs = find_candidate_pairs(
        {
            "gsot36c": "GSOT36C",
            "gsot36ce308": "GSOT36C-E3-08",
            "psot36c": "PSOT36C",
            "ltsr15np": "LTSR15-NP",
        }
    )
    assert ("gsot36c", "gsot36ce308") in pairs  # suffix shape
    assert ("gsot36c", "psot36c") in pairs  # near-miss shape
    assert not any("ltsr15np" in p for p in pairs)  # unrelated


def test_candidates_ignore_long_suffixes():
    pairs = find_candidate_pairs({"abc123": "ABC123", "abc123xxxxxxxx": "ABC123XXXXXXXX"})
    assert pairs == []


def test_formatting_variants_share_a_key_and_never_reach_ai():
    assert normalize_mpn_key("LTSR15-NP") == normalize_mpn_key("LTSR 15-NP")
    pairs = find_candidate_pairs({normalize_mpn_key("LTSR15-NP"): "LTSR15-NP"})
    assert pairs == []


# ── Classifier storage ───────────────────────────────────────────────────


@pytest.mark.anyio
async def test_classify_stores_verdicts_once(db_session):
    spellings = {"gsot36c": "GSOT36C", "gsot36ce308": "GSOT36C-E3-08"}
    with patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
        return_value={"verdict": "same", "confidence": 0.92, "reason": "Vishay -E3-08 = lead-free tape/reel"},
    ) as mock_ai:
        stored = await classify_new_pairs(db_session, spellings)
    assert stored == 1
    row = db_session.query(PartEquivalence).one()
    assert row.verdict == "same"
    assert row.source == "ai"

    with patch("app.utils.claude_client.claude_json", new_callable=AsyncMock) as mock_ai2:
        again = await classify_new_pairs(db_session, spellings)
    assert again == 0  # cached — the model is never re-asked
    mock_ai2.assert_not_called()


@pytest.mark.anyio
async def test_classify_bad_verdict_becomes_uncertain(db_session):
    with patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
        return_value={"verdict": "maybe??", "confidence": 0.5, "reason": "?"},
    ):
        await classify_new_pairs(db_session, {"psot36c": "PSOT36C", "gsot36c": "GSOT36C"})
    assert db_session.query(PartEquivalence).one().verdict == "uncertain"


@pytest.mark.anyio
async def test_classify_offloads_candidate_scan_but_keeps_verdicts(db_session):
    """find_candidate_pairs now runs in an executor (perf); verdicts are unchanged.

    A modest multi-key window yields one suffix candidate pair; the stored verdict and
    reason must match the model's reply exactly — proof the thread offload did not alter
    classification outputs.
    """
    spellings = {"ltc1234": "LTC1234", "ltc1234e3": "LTC1234-E3", "unrelated9": "UNRELATED9"}
    with patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
        return_value={"verdict": "same", "confidence": 0.9, "reason": "LTC -E3 = lead-free packaging code"},
    ):
        stored = await classify_new_pairs(db_session, spellings)
    assert stored == 1
    row = db_session.query(PartEquivalence).one()
    assert (row.key_a, row.key_b) == ("ltc1234", "ltc1234e3")
    assert row.verdict == "same"
    assert row.source == "ai"
    assert row.reason == "LTC -E3 = lead-free packaging code"


# ── Class expansion ──────────────────────────────────────────────────────


def test_expand_part_defaults_to_own_key(db_session):
    eq = expand_part(db_session, "GSOT36C-E3-08")
    assert eq["keys"] == [norm_key("GSOT36C-E3-08")]
    assert eq["ai_variants"] == {}


def test_expand_part_pools_same_and_blocks_different_and_uncertain(db_session):
    _eq(db_session, "GSOT36C", "GSOT36C-E3-08", "same")
    _eq(db_session, "GSOT36C", "PSOT36C", "different")
    _eq(db_session, "GSOT36C", "GSOT36A", "uncertain")
    eq = expand_part(db_session, "GSOT36C")
    assert set(eq["keys"]) == {"gsot36c", "gsot36ce308"}
    assert "gsot36ce308" in eq["ai_variants"]


def test_human_different_outranks_ai_same(db_session):
    _eq(db_session, "GSOT36C", "GSOT36C-E3-08", "same", source="ai")
    user = User(email="rep@trio.com", name="Rep", role="sales", azure_id="az-r")
    db_session.add(user)
    db_session.flush()
    record_human_verdict(db_session, user, "GSOT36C", "GSOT36C-E3-08", "different")
    db_session.commit()
    eq = expand_part(db_session, "GSOT36C")
    assert eq["keys"] == ["gsot36c"]  # un-pooled permanently


def test_record_human_verdict_validates(db_session):
    user = User(email="rep2@trio.com", name="Rep", role="sales", azure_id="az-r2")
    db_session.add(user)
    db_session.flush()
    with pytest.raises(ValueError):
        record_human_verdict(db_session, user, "ABC", "ABC", "different")
    with pytest.raises(ValueError):
        record_human_verdict(db_session, user, "ABC", "DEF", "maybe")


# ── Engine pooling ───────────────────────────────────────────────────────


def _demand(db, mpn, *, qty=1000, days_ago=10):
    rep = db.query(User).filter(User.email == "rep@x.com").first()
    if not rep:
        rep = User(email="rep@x.com", name="Rep X", role="sales", azure_id="az-x")
        db.add(rep)
        db.flush()
    company = db.query(Company).filter(Company.name == "Beckhoff").first()
    if not company:
        company = Company(name="Beckhoff", is_active=True, account_owner_id=rep.id)
        db.add(company)
        db.flush()
        db.add(CustomerSite(company_id=company.id, site_name="HQ", is_active=True))
        db.flush()
    site = db.query(CustomerSite).filter(CustomerSite.company_id == company.id).first()
    req = Requisition(name=f"r-{mpn}-{days_ago}", customer_site_id=site.id, status="open", created_by=rep.id)
    db.add(req)
    db.flush()
    db.add(
        Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            normalized_mpn=normalize_mpn_key(mpn),
            target_qty=qty,
            created_at=datetime.now(UTC) - timedelta(days=days_ago),
        )
    )
    db.commit()
    return company


def _supply(db, mpn, *, qty, price):
    o = Offer(
        vendor_name="Arrow",
        mpn=mpn,
        normalized_mpn=normalize_mpn_key(mpn),
        qty_available=qty,
        unit_price=Decimal(str(price)),
        status="active",
    )
    db.add(o)
    db.commit()
    return o


def test_formatting_variants_pool_with_gray_flag(db_session):
    _demand(db_session, "LTSR15-NP")
    _supply(db_session, "LTSR15-NP", qty=4850, price="6.44")
    _supply(db_session, "LTSR 15-NP", qty=1000, price="10.00")
    r = compute_offer_rollup(db_session, part="LTSR15-NP")
    assert r["available_qty"] == 5850
    assert r["low_cost"] == 6.44
    assert r["variants"]["LTSR 15-NP"]["kind"] == "formatting"
    assert r["has_ai_variants"] is False


def test_ai_same_pools_with_amber_flag_and_matches(db_session):
    _eq(db_session, "GSOT36C", "GSOT36C-E3-08", "same")
    _demand(db_session, "GSOT36C", qty=500_000, days_ago=5)
    offer = _supply(db_session, "GSOT36C-E3-08", qty=177_630, price="0.05")

    r = compute_offer_rollup(db_session, part="GSOT36C")
    assert r["available_qty"] == 177_630
    assert r["has_ai_variants"] is True
    assert r["variants"]["GSOT36C-E3-08"]["kind"] == "ai"

    # The variant offer seeds a match against the GSOT36C ask.
    matches = find_matches_for_offer(offer.id, db_session)
    assert len(matches) == 1
    assert matches[0].mpn == "GSOT36C"  # customer's own spelling wins the display


def test_no_verdict_means_no_pooling(db_session):
    _demand(db_session, "GSOT36C", qty=500_000)
    offer = _supply(db_session, "PSOT36C", qty=500_000, price="0.01")
    r = compute_offer_rollup(db_session, part="GSOT36C")
    assert r["available_qty"] == 0  # near-miss never pools without a verdict
    assert find_matches_for_offer(offer.id, db_session) == []


def test_rejecting_pair_unpools_it(db_session):
    _eq(db_session, "GSOT36C", "GSOT36C-E3-08", "same")
    _demand(db_session, "GSOT36C")
    _supply(db_session, "GSOT36C-E3-08", qty=100, price="0.05")
    assert compute_offer_rollup(db_session, part="GSOT36C")["available_qty"] == 100

    user = db_session.query(User).filter(User.email == "rep@x.com").one()
    record_human_verdict(db_session, user, "GSOT36C", "GSOT36C-E3-08", "different")
    db_session.commit()
    assert compute_offer_rollup(db_session, part="GSOT36C")["available_qty"] == 0


# ── Verdict route ────────────────────────────────────────────────────────


def test_verdict_endpoint(db_session):
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.dependencies import require_user
    from app.main import app

    _eq(db_session, "GSOT36C", "GSOT36C-E3-08", "same")
    user = User(email="rep3@trio.com", name="Rep3", role="sales", azure_id="az-3")
    db_session.add(user)
    db_session.commit()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_user] = lambda: user
    try:
        client = TestClient(app, raise_server_exceptions=False)
        r = client.post(
            "/v2/partials/proactive/equivalence/verdict",
            data={"part_a": "GSOT36C", "part_b": "GSOT36C-E3-08", "verdict": "different"},
            headers=HX,
        )
        assert r.status_code == 200
        row = db_session.query(PartEquivalence).one()
        assert row.verdict == "different"
        assert row.source == "human"
    finally:
        app.dependency_overrides.clear()


# ── QC 2026-08-10 P4: human 'different' is a HARD kill-switch, even transitively ──


def test_human_different_blocks_transitive_repool(db_session):
    """A~C and C~B via AI 'same', but A vs B is human 'different'.

    A must NOT pool B transitively (the kill-switch bug: the human override was silently
    bypassed).
    """
    from app.services.part_equivalence import expand_part, norm_key, record_human_verdict

    # AI says A~C and C~B (a chain).
    _eq(db_session, "LM317T", "LM317TX", "same", source="ai")
    _eq(db_session, "LM317TX", "LM317TY", "same", source="ai")
    # A human ruled A ('LM317T') != B ('LM317TY').
    from app.models import User

    user = User(email="h@x.com", name="H", role="admin", azure_id="az-h")
    db_session.add(user)
    db_session.flush()
    record_human_verdict(db_session, user, "LM317T", "LM317TY", "different")
    db_session.commit()  # fixture is autoflush=False — the verdict must be visible to _load_verdicts

    eq = expand_part(db_session, "LM317T")
    keys = set(eq["keys"])
    assert norm_key("LM317TX") in keys  # the AI 'same' variant still pools
    assert norm_key("LM317TY") not in keys  # but NOT the human-'different' one, transitively
    assert norm_key("LM317T") in keys

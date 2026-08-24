"""test_equivalence_search_recall.py — TDD tests for equivalence-aware search recall
(survey idea #21).

Covers:
  - fast_search pools stored part_equivalences 'same' verdicts into the MPN match
    (an offer stored under a variant key surfaces when the query's class includes
    it) and returns an `equivalence` block naming the variants; a human
    'different' verdict keeps the variant OUT; no verdicts → unchanged shape.
  - The full search-results page renders the variants banner with the amber chip
    + the existing one-tap 'not the same part' verdict form (AI variants only).
  - Zero-hit full-page search enqueues a background classify sweep (throttled per
    user); the type-ahead endpoint NEVER enqueues.
  - The part-dossier hero shows the 'Also known as' variants.

No LLM runs at query time — expansion is a stored-table join (the sanctioned
part-number-only pooling table).

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_equivalence_search_recall.py -v)
Depends on: app.services.global_search_service, app.services.part_equivalence,
            app.routers.htmx.search_views, app.routers.part_dossier, conftest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.constants import OfferStatus
from app.models import Offer, Requirement, User
from app.models.intelligence import PartEquivalence
from app.models.sourcing import Requisition
from app.services.global_search_service import fast_search

_HX = {"HX-Request": "true"}


def _seed_offer(db: Session, user: User, mpn: str, norm: str) -> Offer:
    req = Requisition(
        name=f"REQ-EQ-{norm[:8]}",
        status="open",
        customer_name="AcmeCo",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.flush()
    rq = Requirement(requisition_id=req.id, primary_mpn=mpn, normalized_mpn=norm, created_at=datetime.now(UTC))
    db.add(rq)
    db.flush()
    off = Offer(
        requirement_id=rq.id,
        vendor_name="Var Vendor",
        vendor_name_normalized="var vendor",
        mpn=mpn,
        normalized_mpn=norm,
        unit_price=2.0,
        status=OfferStatus.ACTIVE.value,
    )
    db.add(off)
    db.commit()
    return off


def _verdict(db: Session, a: str, b: str, verdict: str, source: str = "ai") -> None:
    key_a, key_b = sorted((a, b))
    db.add(
        PartEquivalence(
            key_a=key_a,
            key_b=key_b,
            example_a=key_a,
            example_b=key_b,
            verdict=verdict,
            source=source,
            reason="suffix variant" if verdict == "same" else "different family",
        )
    )
    db.commit()


# ── Service: fast_search pooling ───────────────────────────────────────────────


class TestFastSearchEquivalence:
    def test_ai_same_verdict_pools_variant_offer(self, db_session, test_user):
        """Searching LTSR15NP surfaces the offer stored under LTSR15NPR."""
        _seed_offer(db_session, test_user, "LTSR15-NP", "ltsr15np")
        _verdict(db_session, "ltsr15np", "ltsr15npr", "same", source="ai")

        results = fast_search("LTSR15-NPR", db_session, test_user)
        offers = results["groups"].get("offers", [])
        assert any(o.get("mpn") == "LTSR15-NP" for o in offers)
        eq = results.get("equivalence")
        assert eq is not None
        variants = {v["spelling"]: v for v in eq["variants"]}
        assert "LTSR15-NP" in variants  # the observed raw spelling for the variant key
        assert all(v["kind"] == "ai" for v in variants.values())

    def test_human_different_verdict_never_pools(self, db_session, test_user):
        _seed_offer(db_session, test_user, "LTSR15-NP", "ltsr15np")
        _verdict(db_session, "ltsr15np", "ltsr15npr", "different", source="human")

        results = fast_search("LTSR15-NPR", db_session, test_user)
        offers = results["groups"].get("offers", [])
        assert not any(o.get("mpn") == "LTSR15-NP" for o in offers)
        assert not results.get("equivalence")

    def test_no_verdicts_leaves_shape_unchanged(self, db_session, test_user):
        _seed_offer(db_session, test_user, "LM317T", "lm317t")
        results = fast_search("LM317T", db_session, test_user)
        assert any(o.get("mpn") == "LM317T" for o in results["groups"].get("offers", []))
        assert results.get("equivalence") is None

    def test_human_same_verdict_variant_kind_is_human(self, db_session, test_user):
        _seed_offer(db_session, test_user, "LTSR15-NP", "ltsr15np")
        _verdict(db_session, "ltsr15np", "ltsr15npr", "same", source="human")
        results = fast_search("LTSR15-NPR", db_session, test_user)
        eq = results.get("equivalence")
        assert eq is not None
        assert all(v["kind"] == "human" for v in eq["variants"])


# ── Full results page: banner + verdict chip ───────────────────────────────────


class TestResultsPageBanner:
    def test_variants_banner_with_verdict_form(self, client, db_session, test_user):
        _seed_offer(db_session, test_user, "LTSR15-NP", "ltsr15np")
        _verdict(db_session, "ltsr15np", "ltsr15npr", "same", source="ai")

        resp = client.get("/v2/partials/search/results?q=LTSR15-NPR", headers=_HX)
        assert resp.status_code == 200
        body = resp.text
        assert "Also matching" in body or "also known as" in body.lower()
        assert "LTSR15-NP" in body
        # AI variants carry the one-tap 'not the same part' demote button
        # (conformant hx-post button with hx-vals — no form nested in the chip span).
        assert "/v2/partials/proactive/equivalence/verdict" in body
        assert "hx-vals" in body
        assert "part_b" in body

    def test_human_variant_has_no_verdict_form(self, client, db_session, test_user):
        _seed_offer(db_session, test_user, "LTSR15-NP", "ltsr15np")
        _verdict(db_session, "ltsr15np", "ltsr15npr", "same", source="human")
        resp = client.get("/v2/partials/search/results?q=LTSR15-NPR", headers=_HX)
        body = resp.text
        assert "LTSR15-NP" in body
        assert "/v2/partials/proactive/equivalence/verdict" not in body


# ── Zero-hit enqueue ───────────────────────────────────────────────────────────


class TestZeroHitEnqueue:
    def test_results_page_zero_hit_enqueues_sweep(self, client, db_session, test_user):
        with patch("app.routers.htmx.search_views._zero_hit_equivalence_sweep", new_callable=AsyncMock) as sweep:
            resp = client.get("/v2/partials/search/results?q=ZZ99XX77", headers=_HX)
        assert resp.status_code == 200
        sweep.assert_called_once()
        assert sweep.call_args.args[0] == "ZZ99XX77"

    def test_results_page_hit_does_not_enqueue(self, client, db_session, test_user):
        _seed_offer(db_session, test_user, "LM317T", "lm317t")
        with patch("app.routers.htmx.search_views._zero_hit_equivalence_sweep", new_callable=AsyncMock) as sweep:
            client.get("/v2/partials/search/results?q=LM317T", headers=_HX)
        sweep.assert_not_called()

    def test_typeahead_zero_hit_never_enqueues(self, client, db_session, test_user):
        with patch("app.routers.htmx.search_views._zero_hit_equivalence_sweep", new_callable=AsyncMock) as sweep:
            client.get("/v2/partials/search/global?q=ZZ99XX77", headers=_HX)
        sweep.assert_not_called()

    def test_zero_hit_enqueue_rate_limited(self, client, db_session, test_user):
        with (
            patch("app.routers.htmx.search_views.check_rate_limit", return_value=False),
            patch("app.routers.htmx.search_views._zero_hit_equivalence_sweep", new_callable=AsyncMock) as sweep,
        ):
            client.get("/v2/partials/search/results?q=ZZ99XX77", headers=_HX)
        sweep.assert_not_called()

    def test_non_mpn_query_does_not_enqueue(self, client, db_session, test_user):
        """A vendor-name-ish query with no usable MPN key must not burn AI calls."""
        with patch("app.routers.htmx.search_views._zero_hit_equivalence_sweep", new_callable=AsyncMock) as sweep:
            client.get("/v2/partials/search/results?q=ac", headers=_HX)
        sweep.assert_not_called()


# ── Part dossier hero ──────────────────────────────────────────────────────────


def test_dossier_hero_shows_also_known_as(client, db_session, test_user):
    _seed_offer(db_session, test_user, "LTSR15-NP", "ltsr15np")
    _verdict(db_session, "ltsr15np", "ltsr15npr", "same", source="ai")
    resp = client.get("/v2/partials/search/dossier/hero?mpn=LTSR15-NPR", headers=_HX)
    assert resp.status_code == 200
    body = resp.text
    assert "known as" in body.lower()
    assert "LTSR15-NP" in body
    assert "/v2/partials/proactive/equivalence/verdict" in body


# ── Adversarial-review fixes (13 confirmed findings) ───────────────────────────


class TestReviewFixes:
    def test_word_query_zero_hit_does_not_enqueue(self, client, db_session, test_user):
        """'arrow' has >=4 chars but no digit — vendor/contact words never burn AI."""
        with patch("app.routers.htmx.search_views._zero_hit_equivalence_sweep", new_callable=AsyncMock) as sweep:
            client.get("/v2/partials/search/results?q=arrow", headers=_HX)
        sweep.assert_not_called()

    def test_sweep_uses_safe_background_task(self, client, db_session, test_user):
        """The sweep goes through the canonical strong-ref/error-isolated holder (P0.4
        pattern) — never a bare asyncio.create_task."""
        with (
            patch("app.routers.htmx.search_views.safe_background_task", new_callable=AsyncMock) as sbt,
            patch("app.routers.htmx.search_views._zero_hit_equivalence_sweep", new_callable=AsyncMock),
        ):
            client.get("/v2/partials/search/results?q=ZZ99XX77", headers=_HX)
        sbt.assert_called_once()

    async def test_classify_involving_filters_pairs(self, db_session):
        """Involving= caps the sweep to pairs touching the missed key — both the spend
        bound and the aim-at-the-searched-part fix."""
        from app.services.part_equivalence import classify_new_pairs

        spellings = {"lm317": "LM317", "lm317t": "LM317T", "zz991": "ZZ991", "zz991x": "ZZ991X"}
        with patch(
            "app.utils.claude_client.claude_json",
            new_callable=AsyncMock,
            return_value={"verdict": "same", "confidence": 0.9, "reason": "suffix"},
        ) as cj:
            stored = await classify_new_pairs(db_session, spellings, limit=5, involving="lm317")
        assert stored == 1
        assert cj.call_count == 1
        rows = db_session.query(PartEquivalence).all()
        assert {(r.key_a, r.key_b) for r in rows} == {("lm317", "lm317t")}

    def test_ai_pooled_variant_stays_amber_despite_adjacent_human_edge(self, db_session, test_user):
        """Review repro: aaa111x pooled ONLY via an AI edge must render kind='ai'
        (verify chip shown) even though a human 'same' edge touches it elsewhere."""
        _seed_offer(db_session, test_user, "AAA-111X", "aaa111x")
        _verdict(db_session, "aaa111", "aaa111x", "same", source="ai")
        _verdict(db_session, "aaa111x", "aaa111xz", "same", source="human")

        results = fast_search("AAA-111", db_session, test_user)
        eq = results.get("equivalence")
        assert eq is not None
        kind_by_key = {v["key"]: v["kind"] for v in eq["variants"]}
        assert kind_by_key["aaa111x"] == "ai"
        assert kind_by_key["aaa111xz"] == "human"

    def test_restricted_user_variant_spelling_not_from_foreign_offer(self, db_session, test_user, sales_user):
        """RESTRICTED_ROLES never see foreign offers' raw spellings in the banner — the
        spelling falls back to the verdict row's own example."""
        _seed_offer(db_session, test_user, "SECRET-15X-RAW", "secret15x")
        key_a, key_b = sorted(("secret15", "secret15x"))
        db_session.add(
            PartEquivalence(
                key_a=key_a,
                key_b=key_b,
                example_a="SECRET-15",
                example_b="SECRET-15X",
                verdict="same",
                source="ai",
                reason="suffix",
            )
        )
        db_session.commit()

        results = fast_search("SECRET-15", db_session, sales_user)
        assert not results["groups"].get("offers")  # scope-gated as before
        eq = results.get("equivalence")
        assert eq is not None
        spellings = [v["spelling"] for v in eq["variants"]]
        assert "SECRET-15X-RAW" not in spellings
        assert "SECRET-15X" in spellings

    def test_banner_hidden_on_zero_result_page(self, client, db_session, test_user):
        """A verdict with no matching rows must not claim 'Also matching…' on an empty
        results page."""
        _verdict(db_session, "ltsr15np", "ltsr15npr", "same", source="ai")
        resp = client.get("/v2/partials/search/results?q=LTSR15-NPR", headers=_HX)
        assert resp.status_code == 200
        assert "Also matching" not in resp.text

    def test_typeahead_dropdown_labels_variants(self, client, db_session, test_user):
        _seed_offer(db_session, test_user, "LTSR15-NP", "ltsr15np")
        _verdict(db_session, "ltsr15np", "ltsr15npr", "same", source="ai")
        resp = client.get("/v2/partials/search/global?q=LTSR15-NPR", headers=_HX)
        assert resp.status_code == 200
        assert "Includes known variants" in resp.text

    def test_verdict_button_hidden_without_proactive_access(self, client, db_session, test_user):
        _seed_offer(db_session, test_user, "LTSR15-NP", "ltsr15np")
        _verdict(db_session, "ltsr15np", "ltsr15npr", "same", source="ai")
        with patch("app.routers.htmx.search_views.user_has_access", return_value=False):
            resp = client.get("/v2/partials/search/results?q=LTSR15-NPR", headers=_HX)
        assert "LTSR15-NP" in resp.text  # banner still informs
        assert "/v2/partials/proactive/equivalence/verdict" not in resp.text

    def test_verdict_origin_search_returns_inline_snippet(self, client, db_session, test_user):
        """Demoting from Search must NOT teleport into the Proactive Matches tab —
        origin=search gets a tiny inline replacement for the chip."""
        resp = client.post(
            "/v2/partials/proactive/equivalence/verdict",
            data={"part_a": "LTSR15-NPR", "part_b": "LTSR15-NP", "verdict": "different", "origin": "search"},
            headers=_HX,
        )
        assert resp.status_code == 200
        assert "removed" in resp.text.lower()
        assert len(resp.text) < 600  # a chip-sized snippet, not the Matches tab
        row = db_session.query(PartEquivalence).one()
        assert row.verdict == "different"
        assert row.source == "human"

"""tests/test_sp3_ui.py — TDD tests for SP3 UI: card/detail scores, screened-out bucket,
AI-match sort option, and default sort change.

Uses the shared `client` fixture (auth overridden) + `db_session` fixture from conftest.
All tests are gated: when ai_screen data is absent the UI must be invisible.

Called by: pytest autodiscovery
Depends on: conftest.py (client, db_session), app.models.prospect_account.ProspectAccount
"""

import os

os.environ["TESTING"] = "1"

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.models.prospect_account import ProspectAccount

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_prospect(
    db: Session,
    *,
    name: str | None = None,
    domain: str | None = None,
    enrichment_data: dict | None = None,
    trio_match_score: int = 0,
    opportunity_score: int = 0,
    **kw,
) -> ProspectAccount:
    uid = uuid.uuid4().hex[:6]
    p = ProspectAccount(
        name=name or f"Co {uid}",
        domain=domain or f"co-{uid}.com",
        status=kw.pop("status", "suggested"),
        discovery_source="clay",
        created_at=datetime.now(UTC),
        enrichment_data=enrichment_data or {},
        readiness_signals={},
        trio_match_score=trio_match_score,
        opportunity_score=opportunity_score,
        **kw,
    )
    db.add(p)
    db.commit()
    return p


def _ai_screen(verdict: str = "pass", match: int = 80, opp: int = 65) -> dict:
    return {
        "ai_screen": {
            "verdict": verdict,
            "trio_match_score": match,
            "opportunity_score": opp,
            "excess_likelihood": 20,
            "rationale": "Aerospace OEM with verified procurement contact.",
            "evidence": ["industry=Aerospace", "naics=336412"],
            "confidence": 85,
            "model": "claude-sonnet-4-6",
            "screened_at": "2026-06-18T00:00:00+00:00",
        }
    }


# ── Card: AI scores visible when ai_screen present ────────────────────────────


class TestCardAIScores:
    def test_card_shows_ai_match_score_when_screened(self, client, db_session, monkeypatch):
        """Card renders AI Match score bar when ai_screen verdict is present."""
        monkeypatch.setattr(settings, "ai_screen_enabled", True)
        _make_prospect(
            db_session,
            name="AeroCo",
            trio_match_score=80,
            enrichment_data=_ai_screen(verdict="pass", match=80, opp=65),
        )
        resp = client.get("/v2/partials/prospecting?sort=ai_match_desc")
        assert resp.status_code == 200
        body = resp.text
        # Score value rendered somewhere in the card
        assert "80" in body
        # AI Match label rendered
        assert "AI Match" in body

    def test_card_shows_rationale_when_screened(self, client, db_session, monkeypatch):
        """Card renders the one-line rationale snippet when ai_screen is present."""
        monkeypatch.setattr(settings, "ai_screen_enabled", True)
        _make_prospect(
            db_session,
            name="AeroCo",
            enrichment_data=_ai_screen(verdict="pass"),
        )
        resp = client.get("/v2/partials/prospecting?sort=ai_match_desc")
        assert resp.status_code == 200
        assert "Aerospace OEM with verified procurement contact." in resp.text

    def test_card_hides_ai_scores_when_no_ai_screen(self, client, db_session):
        """Card must NOT render AI Match section when enrichment_data has no
        ai_screen."""
        _make_prospect(
            db_session,
            name="VanillaCo",
            enrichment_data={},  # no ai_screen key
        )
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        # AI Match label must be absent
        assert "AI Match" not in resp.text

    def test_card_hides_ai_scores_for_insufficient_data_verdict(self, client, db_session):
        """Card must NOT render AI Match bar for insufficient_data verdict."""
        _make_prospect(
            db_session,
            name="ThinCo",
            enrichment_data={
                "ai_screen": {
                    "verdict": "insufficient_data",
                    "trio_match_score": 0,
                    "rationale": "No data.",
                }
            },
        )
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert "AI Match" not in resp.text


# ── Detail: AI Screening card visible when ai_screen present ──────────────────


class TestDetailAIScreening:
    def test_detail_shows_ai_screening_section_when_screened(self, client, db_session):
        """Detail renders an 'AI Screening' section when ai_screen verdict is
        present."""
        p = _make_prospect(
            db_session,
            name="AeroCo",
            trio_match_score=80,
            opportunity_score=65,
            enrichment_data=_ai_screen(verdict="pass", match=80, opp=65),
        )
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        body = resp.text
        assert "AI Screening" in body
        assert "Match" in body
        assert "Opportunity" in body
        # Rationale text
        assert "Aerospace OEM with verified procurement contact." in body

    def test_detail_shows_evidence_list(self, client, db_session):
        """Detail renders evidence items from the verdict."""
        p = _make_prospect(
            db_session,
            name="AeroCo",
            enrichment_data=_ai_screen(verdict="pass"),
        )
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        body = resp.text
        assert "industry=Aerospace" in body
        assert "naics=336412" in body

    def test_detail_shows_confidence_and_model(self, client, db_session):
        """Detail renders confidence % and model name."""
        p = _make_prospect(
            db_session,
            name="AeroCo",
            enrichment_data=_ai_screen(verdict="pass"),
        )
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        body = resp.text
        assert "85" in body  # confidence
        assert "claude-sonnet-4-6" in body  # model

    def test_detail_hides_ai_screening_when_absent(self, client, db_session):
        """Detail must NOT render AI Screening section when ai_screen is absent."""
        p = _make_prospect(
            db_session,
            name="VanillaCo",
            enrichment_data={},
        )
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        assert "AI Screening" not in resp.text

    def test_detail_shows_screened_out_verdict_badge(self, client, db_session):
        """Detail renders 'Screened out' badge when verdict is screened_out."""
        p = _make_prospect(
            db_session,
            name="LowFitCo",
            enrichment_data=_ai_screen(verdict="screened_out", match=10),
        )
        resp = client.get(f"/v2/partials/prospecting/{p.id}")
        assert resp.status_code == 200
        # The rendered text should contain the normalized verdict label
        assert "Screened out" in resp.text or "screened_out" in resp.text


# ── List: AI match sort option and screened-out bucket ────────────────────────


class TestListSortAndBucket:
    def test_sort_dropdown_includes_ai_match_option(self, client, db_session):
        """Sort <select> renders 'AI match' option."""
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert 'value="ai_match_desc"' in resp.text
        assert "AI match" in resp.text

    def test_ai_match_sort_is_default_when_no_sort_param(self, client, db_session):
        """Default route (no ?sort=) uses ai_match_desc (option appears selected)."""
        resp = client.get("/v2/partials/prospecting")
        assert resp.status_code == 200
        assert 'value="ai_match_desc"' in resp.text
        # The ai_match option must be the selected one
        body = resp.text
        ai_match_pos = body.index('value="ai_match_desc"')
        selected_near = body[ai_match_pos : ai_match_pos + 60]
        assert "selected" in selected_near

    def test_screened_out_bucket_rendered_when_enabled_and_data_present(self, client, db_session, monkeypatch):
        """Screened-out bucket section appears when ai_screen_enabled and there are
        screened_out accounts."""
        monkeypatch.setattr(settings, "ai_screen_enabled", True)
        _make_prospect(
            db_session,
            name="LowFitCo",
            trio_match_score=10,
            enrichment_data=_ai_screen(verdict="screened_out", match=10),
        )
        resp = client.get("/v2/partials/prospecting?sort=ai_match_desc")
        assert resp.status_code == 200
        body = resp.text
        assert "Screened out" in body or "screened" in body.lower()
        assert "LowFitCo" in body

    def test_screened_out_bucket_claim_anyway_button_present(self, client, db_session, monkeypatch):
        """Each screened-out row has a 'Claim anyway' button wired to the existing claim
        endpoint."""
        monkeypatch.setattr(settings, "ai_screen_enabled", True)
        p = _make_prospect(
            db_session,
            name="LowFitCo",
            trio_match_score=10,
            enrichment_data=_ai_screen(verdict="screened_out", match=10),
        )
        resp = client.get("/v2/partials/prospecting?sort=ai_match_desc")
        assert resp.status_code == 200
        body = resp.text
        # The claim endpoint for this prospect must appear
        assert f"/v2/partials/prospecting/{p.id}/claim" in body
        assert "Claim anyway" in body

    def test_screened_out_row_has_claim_target_id(self, client, db_session, monkeypatch):
        """F4: the screened-out row must carry id='prospect-<id>' so the Claim-anyway
        button's hx-target='#prospect-<id>' resolves. Screened-out prospects are filtered
        out of the grid (where _card.html normally provides that id), so without an id on
        the row htmx aborts the claim with targetError and the button is dead."""
        monkeypatch.setattr(settings, "ai_screen_enabled", True)
        p = _make_prospect(
            db_session,
            name="LowFitCo",
            trio_match_score=10,
            enrichment_data=_ai_screen(verdict="screened_out", match=10),
        )
        resp = client.get("/v2/partials/prospecting?sort=ai_match_desc")
        assert resp.status_code == 200
        body = resp.text
        assert f'id="prospect-{p.id}"' in body  # target the claim button swaps
        assert f'hx-target="#prospect-{p.id}"' in body  # button points at that id

    def test_screened_out_bucket_hidden_when_screening_disabled(self, client, db_session, monkeypatch):
        """No screened-out section when ai_screen_enabled=False."""
        monkeypatch.setattr(settings, "ai_screen_enabled", False)
        _make_prospect(
            db_session,
            name="LowFitCo",
            enrichment_data=_ai_screen(verdict="screened_out", match=10),
        )
        resp = client.get("/v2/partials/prospecting?sort=ai_match_desc")
        assert resp.status_code == 200
        assert "Claim anyway" not in resp.text

    def test_screened_out_bucket_hidden_when_sort_not_ai_match(self, client, db_session, monkeypatch):
        """No screened-out section when sort is not ai_match_desc."""
        monkeypatch.setattr(settings, "ai_screen_enabled", True)
        _make_prospect(
            db_session,
            name="LowFitCo",
            enrichment_data=_ai_screen(verdict="screened_out", match=10),
        )
        resp = client.get("/v2/partials/prospecting?sort=buyer_ready_desc")
        assert resp.status_code == 200
        assert "Claim anyway" not in resp.text

    def test_ai_match_sort_ranks_highest_match_first(self, client, db_session, monkeypatch):
        """ai_match_desc sort renders higher-match prospects before lower ones."""
        monkeypatch.setattr(settings, "ai_screen_enabled", True)
        _make_prospect(
            db_session,
            name="HighMatchCo",
            trio_match_score=90,
            enrichment_data=_ai_screen(verdict="pass", match=90),
        )
        _make_prospect(
            db_session,
            name="LowMatchCo",
            trio_match_score=20,
            enrichment_data=_ai_screen(verdict="pass", match=20),
        )
        resp = client.get("/v2/partials/prospecting?sort=ai_match_desc")
        assert resp.status_code == 200
        body = resp.text
        assert body.index("HighMatchCo") < body.index("LowMatchCo")
